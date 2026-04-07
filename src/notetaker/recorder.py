from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from datetime import date

import requests

log = logging.getLogger(__name__)

OCS_HEADERS = {"OCS-APIRequest": "true", "Accept": "application/json"}

# JavaScript injected into the browser BEFORE Talk loads.
# Intercepts RTCPeerConnection to capture all remote audio streams
# via Web Audio API + MediaRecorder.
AUDIO_CAPTURE_INIT_JS = """
(() => {
    window.__notetaker_chunks = [];
    window.__notetaker_recorder = null;
    window.__notetaker_ctx = null;
    window.__notetaker_dest = null;
    window.__notetaker_pc_count = 0;
    window.__notetaker_track_count = 0;
    window.__notetaker_gum_count = 0;
    window.__notetaker_wrapped_pcs = new WeakSet();

    // === SPEAKER TIMELINE (per-track VAD) ===
    window.__nt_timeline = [];   // {streamId, startMs, endMs}
    window.__nt_t0 = null;
    window.__nt_peers = {};      // streamId -> {analyser, active, startedAt, belowSince}
    window.__nt_streamEl = {};   // streamId -> <video> element (for name lookup via DOM)
    window.__nt_vadInterval = null;
    const VAD_THRESHOLD = 0.02;  // RMS threshold (0..1)
    const VAD_HANGOVER_MS = 400; // silence duration that ends an utterance

    function startVadLoop() {
        if (window.__nt_vadInterval) return;
        window.__nt_vadInterval = setInterval(() => {
            const now = performance.now();
            if (window.__nt_t0 === null) window.__nt_t0 = now;
            for (const key of Object.keys(window.__nt_peers)) {
                const peer = window.__nt_peers[key];
                const buf = new Uint8Array(peer.analyser.fftSize);
                peer.analyser.getByteTimeDomainData(buf);
                let sumSq = 0;
                for (let i = 0; i < buf.length; i++) {
                    const v = (buf[i] - 128) / 128;
                    sumSq += v * v;
                }
                const rms = Math.sqrt(sumSq / buf.length);
                const speaking = rms > VAD_THRESHOLD;
                if (speaking) {
                    if (!peer.active) {
                        peer.active = true;
                        peer.startedAt = now;
                    }
                    peer.belowSince = null;
                } else if (peer.active) {
                    if (peer.belowSince === null) {
                        peer.belowSince = now;
                    } else if (now - peer.belowSince > VAD_HANGOVER_MS) {
                        window.__nt_timeline.push({
                            streamId: key,
                            startMs: Math.round(peer.startedAt - window.__nt_t0),
                            endMs: Math.round(peer.belowSince - window.__nt_t0),
                        });
                        peer.active = false;
                        peer.belowSince = null;
                    }
                }
            }
        }, 100);
    }

    // Hook HTMLMediaElement.srcObject so we can map stream.id -> <video> element.
    // Talk's UI always assigns remote streams to a <video> for rendering, and the
    // surrounding DOM has the participant's display name next to it.
    try {
        const desc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'srcObject');
        if (desc && desc.set) {
            Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', {
                configurable: true,
                get: desc.get,
                set(stream) {
                    desc.set.call(this, stream);
                    try {
                        if (stream && stream.id) {
                            window.__nt_streamEl[stream.id] = this;
                        }
                    } catch(e) {}
                }
            });
        }
    } catch(e) {
        console.error('[notetaker] srcObject hook failed:', e);
    }

    function startRecordingAudioTrack(track, streamId) {
        const stream = new MediaStream([track]);
        try {
            if (!window.__notetaker_ctx) {
                window.__notetaker_ctx = new AudioContext({ sampleRate: 48000 });
                console.log('[notetaker] AudioContext created, state=' + window.__notetaker_ctx.state);
            }
            if (!window.__notetaker_dest) {
                window.__notetaker_dest = window.__notetaker_ctx.createMediaStreamDestination();
            }
            if (window.__notetaker_ctx.state === 'suspended') {
                window.__notetaker_ctx.resume().then(() =>
                    console.log('[notetaker] AudioContext resumed'));
            }
            const source = window.__notetaker_ctx.createMediaStreamSource(stream);
            source.connect(window.__notetaker_dest);
            console.log('[notetaker] Connected audio track to recording destination (streamId=' + streamId + ')');

            // Per-track analyser for the speaker timeline
            if (streamId && !window.__nt_peers[streamId]) {
                const analyser = window.__notetaker_ctx.createAnalyser();
                analyser.fftSize = 512;
                analyser.smoothingTimeConstant = 0.2;
                source.connect(analyser);
                window.__nt_peers[streamId] = {
                    analyser, active: false, startedAt: 0, belowSince: null,
                };
                startVadLoop();
                console.log('[notetaker] VAD analyser attached for stream ' + streamId);
            }

            if (!window.__notetaker_recorder) {
                const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                    ? 'audio/webm;codecs=opus' : 'audio/webm';
                window.__notetaker_recorder = new MediaRecorder(
                    window.__notetaker_dest.stream, { mimeType }
                );
                window.__notetaker_recorder.ondataavailable = (e) => {
                    if (e.data.size > 0) window.__notetaker_chunks.push(e.data);
                };
                window.__notetaker_recorder.start(1000);
                console.log('[notetaker] MediaRecorder started (' + mimeType + ')');
            }
        } catch (err) {
            console.error('[notetaker] Audio capture error:', err);
        }
    }

    // Hook a PeerConnection instance to capture audio tracks
    function hookPC(pc) {
        if (window.__notetaker_wrapped_pcs.has(pc)) return;
        window.__notetaker_wrapped_pcs.add(pc);
        window.__notetaker_pc_count++;
        console.log('[notetaker] Hooked PeerConnection #' + window.__notetaker_pc_count +
            ' signalingState=' + pc.signalingState);

        pc.addEventListener('track', (event) => {
            window.__notetaker_track_count++;
            console.log('[notetaker] Track event: kind=' + event.track.kind +
                ' readyState=' + event.track.readyState +
                ' (#' + window.__notetaker_track_count + ')');
            if (event.track.kind === 'audio') {
                const sid = (event.streams && event.streams[0]) ? event.streams[0].id : ('pc' + window.__notetaker_pc_count);
                startRecordingAudioTrack(event.track, sid);
            }
        });
    }

    // === INTERCEPTION STRATEGY ===
    // Instead of only proxying the constructor (which Talk's bundle may bypass),
    // patch RTCPeerConnection.prototype methods that MUST be called on any PC.
    // This works regardless of how the PC was constructed.

    const origProto = RTCPeerConnection.prototype;

    // Patch setRemoteDescription — always called to establish a connection
    const origSetRemote = origProto.setRemoteDescription;
    origProto.setRemoteDescription = function(desc) {
        console.log('[notetaker] setRemoteDescription called, type=' +
            (desc ? desc.type : 'null'));
        hookPC(this);
        return origSetRemote.apply(this, arguments);
    };

    // Patch setLocalDescription — also always called
    const origSetLocal = origProto.setLocalDescription;
    origProto.setLocalDescription = function(desc) {
        console.log('[notetaker] setLocalDescription called, type=' +
            (desc ? (desc.type || 'implicit') : 'null'));
        hookPC(this);
        return origSetLocal.apply(this, arguments);
    };

    // Patch addTrack / addTransceiver — called when adding local media
    const origAddTrack = origProto.addTrack;
    origProto.addTrack = function(track, ...streams) {
        console.log('[notetaker] addTrack called, kind=' + track.kind);
        hookPC(this);
        return origAddTrack.apply(this, arguments);
    };

    // Also still proxy the constructor as a belt-and-suspenders approach
    const OrigRTC = window.RTCPeerConnection;
    const ProxiedRTC = new Proxy(OrigRTC, {
        construct(target, args) {
            const pc = Reflect.construct(target, args);
            hookPC(pc);
            return pc;
        }
    });
    ProxiedRTC.prototype = OrigRTC.prototype;
    window.RTCPeerConnection = ProxiedRTC;

    // Intercept getUserMedia to return synthetic silent/black streams
    // instead of the fake device streams (which produce 440Hz beep + pacman).
    // We don't use the fake device output at all — we generate our own
    // silent audio (via oscillator with zero gain) and black video (via canvas).
    // This is immune to Talk re-enabling tracks since the source itself is silent.
    const origGUM = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = function(constraints) {
        window.__notetaker_gum_count++;
        console.log('[notetaker] getUserMedia #' + window.__notetaker_gum_count +
            ': ' + JSON.stringify(constraints));

        const tracks = [];

        // Create silent audio track via Web Audio API
        if (constraints && (constraints.audio || constraints.audio === true ||
            (typeof constraints.audio === 'object'))) {
            try {
                const audioCtx = new AudioContext();
                const oscillator = audioCtx.createOscillator();
                const gain = audioCtx.createGain();
                gain.gain.value = 0; // silence
                oscillator.connect(gain);
                const dest = audioCtx.createMediaStreamDestination();
                gain.connect(dest);
                oscillator.start();
                dest.stream.getAudioTracks().forEach(t => tracks.push(t));
                console.log('[notetaker] Created silent audio track');
            } catch(e) {
                console.error('[notetaker] Failed to create silent audio:', e);
            }
        }

        // Create black video track via canvas
        if (constraints && (constraints.video || constraints.video === true ||
            (typeof constraints.video === 'object'))) {
            try {
                const canvas = document.createElement('canvas');
                canvas.width = 640;
                canvas.height = 480;
                const ctx2d = canvas.getContext('2d');
                ctx2d.fillStyle = 'black';
                ctx2d.fillRect(0, 0, 640, 480);
                const canvasStream = canvas.captureStream(1); // 1 fps
                canvasStream.getVideoTracks().forEach(t => tracks.push(t));
                console.log('[notetaker] Created black video track');
            } catch(e) {
                console.error('[notetaker] Failed to create black video:', e);
            }
        }

        if (tracks.length > 0) {
            const silentStream = new MediaStream(tracks);
            console.log('[notetaker] Returning synthetic stream with ' +
                tracks.length + ' tracks');
            return Promise.resolve(silentStream);
        }

        // Fallback: if we couldn't create synthetic tracks, use original
        // but disable them (less reliable but better than nothing)
        return origGUM(constraints).then(stream => {
            stream.getTracks().forEach(track => {
                track.enabled = false;
                console.log('[notetaker] Fallback: disabled ' + track.kind + ' track');
            });
            return stream;
        });
    };

    console.log('[notetaker] Audio hooks installed (prototype + proxy + gUM)');
})();
"""

# JavaScript to flush accumulated audio chunks as base64 and clear the buffer.
# Called periodically during recording to bound browser memory usage.
FLUSH_CHUNKS_JS = """
async () => {
    const chunks = window.__notetaker_chunks;
    if (chunks.length === 0) return null;
    const blob = new Blob(chunks, { type: 'audio/webm' });
    window.__notetaker_chunks = [];
    const reader = new FileReader();
    return new Promise(resolve => {
        reader.onloadend = () => resolve(reader.result.split(',')[1]);
        reader.readAsDataURL(blob);
    });
}
"""

# JavaScript to extract the speaker timeline + stream->displayName mapping via DOM.
# Called once, after the call has ended.
EXTRACT_TIMELINE_JS = """
async () => {
    // Flush any in-progress utterances
    const now = performance.now();
    for (const key of Object.keys(window.__nt_peers || {})) {
        const peer = window.__nt_peers[key];
        if (peer.active && window.__nt_t0 !== null) {
            window.__nt_timeline.push({
                streamId: key,
                startMs: Math.round(peer.startedAt - window.__nt_t0),
                endMs: Math.round(now - window.__nt_t0),
            });
            peer.active = false;
        }
    }

    // Resolve stream.id -> display name by walking the DOM from the <video>
    // element whose srcObject carries that stream.
    const labels = {};
    for (const streamId of Object.keys(window.__nt_streamEl || {})) {
        const el = window.__nt_streamEl[streamId];
        let name = null;
        let node = el;
        for (let depth = 0; depth < 10 && node; depth++) {
            if (node.querySelector) {
                const cand = node.querySelector('.nameIndicator, .participant-name, [class*="nameIndicator"], [class*="participantName"], [data-participant-name]');
                if (cand && cand.textContent && cand.textContent.trim()) {
                    name = cand.textContent.trim();
                    break;
                }
                // Last-resort: any descendant element whose data-* attribute looks like a name
                const dn = node.querySelector('[data-display-name]');
                if (dn) {
                    name = dn.getAttribute('data-display-name');
                    if (name) break;
                }
            }
            node = node.parentElement;
        }
        labels[streamId] = name;
    }
    return {
        timeline: window.__nt_timeline || [],
        labels: labels,
        streamIds: Object.keys(window.__nt_peers || {}),
    };
}
"""

# JavaScript to stop recording and extract remaining audio as base64
EXTRACT_AUDIO_JS = """
async () => {
    if (window.__notetaker_recorder && window.__notetaker_recorder.state !== 'inactive') {
        window.__notetaker_recorder.stop();
        await new Promise(r => setTimeout(r, 500));
    }
    const chunks = window.__notetaker_chunks;
    if (chunks.length === 0) return null;
    const blob = new Blob(chunks, { type: 'audio/webm' });
    window.__notetaker_chunks = [];
    const reader = new FileReader();
    return new Promise(resolve => {
        reader.onloadend = () => resolve(reader.result.split(',')[1]);
        reader.readAsDataURL(blob);
    });
}
"""


def _slugify(name: str) -> str:
    """Convert a conversation name to a filename-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def _others_in_call(base_url: str, auth: tuple, room_token: str, own_user: str) -> bool:
    """Check via Talk API if any other participants are still in the call."""
    try:
        resp = requests.get(
            f"{base_url}/ocs/v2.php/apps/spreed/api/v4/room/{room_token}/participants",
            auth=auth,
            headers=OCS_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        for p in resp.json()["ocs"]["data"]:
            if p.get("actorId") == own_user:
                continue
            if p.get("inCall", 0) > 0:
                return True
    except Exception:
        log.warning("Failed to check call participants via API, assuming still active")
        return True
    return False


class AudioRecorder:
    """Records audio from a Nextcloud Talk call via Playwright.

    Launches a headless Chromium browser, logs into Nextcloud as the bot user,
    joins the specified Talk call, and records remote audio using the browser's
    WebRTC + MediaRecorder API (no PulseAudio needed).
    """

    def __init__(
        self,
        nextcloud_url: str,
        user: str,
        password: str,
        audio_dir: str,
        auth_method: str = "nextcloud",
    ) -> None:
        self.nextcloud_url = nextcloud_url.rstrip("/")
        self.user = user
        self.password = password
        self.audio_dir = audio_dir
        self.auth_method = auth_method
        os.makedirs(audio_dir, exist_ok=True)

    def _output_path(self, token: str, conversation_name: str) -> str:
        """Build the output audio file path for a recording."""
        slug = _slugify(conversation_name) or token
        filename = f"{date.today().isoformat()}-{slug}.webm"
        return os.path.join(self.audio_dir, filename)

    async def record_call(
        self, room_token: str, conversation_name: str
    ) -> tuple[str, list[dict]]:
        """Join a Talk call, record audio, return path to audio file.

        Blocks until the call ends (all other participants leave).
        Uses in-browser MediaRecorder to capture WebRTC audio directly.
        """
        output_path = self._output_path(room_token, conversation_name)

        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    "--autoplay-policy=no-user-gesture-required",
                    "--no-sandbox",
                    "--disable-gpu",
                ],
            )
            context = await browser.new_context(
                ignore_https_errors=True,
                viewport={"width": 1920, "height": 1080},
                permissions=["microphone", "camera"],
            )

            # Inject audio capture script into every page in this context
            # BEFORE Talk's JavaScript creates RTCPeerConnections
            await context.add_init_script(AUDIO_CAPTURE_INIT_JS)

            page = await context.new_page()

            # Log browser console for debugging audio capture
            page.on("console", lambda msg: log.debug("Browser: %s", msg.text))

            # Log in based on auth_method
            if self.auth_method == "yunohost":
                # Yunohost SSO browser form login
                sso_url = f"{self.nextcloud_url}/yunohost/sso/"
                await page.goto(sso_url)
                await page.wait_for_load_state("networkidle")

                await page.fill(
                    'input[name="username"], input[placeholder*="sername" i]',
                    self.user,
                )
                await page.fill(
                    'input[name="password"], input[type="password"]',
                    self.password,
                )
                await page.click(
                    'button[type="submit"], input[type="submit"], button:has-text("Log in")'
                )
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(2)
                log.info("SSO form login completed, URL: %s", page.url)

                # Navigate to Nextcloud root to establish PHP session
                await page.goto(f"{self.nextcloud_url}/")
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(3)
            else:
                # Standard Nextcloud login (Vue.js SPA — must wait for
                # form elements to render, not just the page load event)
                login_url = f"{self.nextcloud_url}/login"
                await page.goto(login_url)
                await page.wait_for_load_state("load")

                # Wait for the Vue-rendered login form to appear
                user_field = page.locator('#user, input[name="user"]')
                await user_field.first.wait_for(state="visible", timeout=30000)
                log.info("Login form rendered, filling credentials")

                await user_field.first.fill(self.user)
                await page.fill(
                    '#password, input[name="password"], input[type="password"]',
                    self.password,
                )
                await page.click(
                    '#submit-form, button[type="submit"], input[type="submit"]'
                )
                await page.wait_for_load_state("load")

                # Verify login succeeded: wait for redirect away from /login
                # or for the user menu to appear (authenticated state)
                for _check in range(10):
                    await asyncio.sleep(2)
                    current_url = page.url
                    if "/login" not in current_url:
                        log.info("Login succeeded, redirected to: %s", current_url)
                        break
                    # Check for login error messages
                    error_el = page.locator('.warning, .error, [class*="error"]')
                    try:
                        if await error_el.first.is_visible(timeout=500):
                            error_text = await error_el.first.text_content()
                            raise RuntimeError(f"Nextcloud login failed: {error_text}")
                    except RuntimeError:
                        raise
                    except Exception:
                        pass
                else:
                    raise RuntimeError(
                        f"Nextcloud login failed: still on login page after 20s "
                        f"(URL: {page.url}). Check credentials for user '{self.user}'."
                    )

            log.info("Login completed (%s), URL: %s", self.auth_method, page.url)

            # Dismiss first-run wizard / modal overlays
            for _ in range(5):
                close_btn = page.locator(
                    'button[aria-label="Close" i],'
                    ' button:has-text("Close"),'
                    ' button:has-text("Skip"),'
                    " .modal-container button.modal-container__close"
                )
                try:
                    visible = await close_btn.first.is_visible()
                except Exception:
                    visible = False
                if visible:
                    await close_btn.first.click()
                    await asyncio.sleep(1)
                    log.info("Dismissed a modal/wizard overlay")
                else:
                    break

            # Navigate to the Talk room (use "load" not "networkidle"
            # because Talk keeps WebSocket connections open)
            talk_url = f"{self.nextcloud_url}/call/{room_token}"
            await page.goto(talk_url)
            await page.wait_for_load_state("load")
            await asyncio.sleep(8)

            log.info("Talk page URL: %s", page.url)

            # Dismiss any Talk-specific overlays
            for _ in range(3):
                overlay = page.locator(
                    'button[aria-label="Close" i],'
                    ' button:has-text("Close"),'
                    ' button:has-text("Skip"),'
                    ' button:has-text("Dismiss")'
                )
                try:
                    visible = await overlay.first.is_visible()
                except Exception:
                    visible = False
                if visible:
                    await overlay.first.click()
                    await asyncio.sleep(1)
                    log.info("Dismissed Talk overlay")
                else:
                    break

            # Verify our hooks are installed before joining
            pre_diag = await page.evaluate("""() => ({
                proxyInstalled: typeof window.__notetaker_pc_count === 'number',
                rtcType: typeof RTCPeerConnection,
                rtcAvailable: typeof RTCPeerConnection !== 'undefined',
                gumAvailable: typeof navigator.mediaDevices?.getUserMedia === 'function',
            })""")
            log.info("Pre-join diagnostics: %s", pre_diag)

            # Join the call — Talk has a two-step process:
            # 1. Click "Join call" in the top bar → opens media settings dialog
            # 2. Click "Join call" inside the dialog → actually joins the call
            join_btn = page.locator(
                'button:has-text("Join call"),'
                ' button:has-text("Start call"),'
                ' [data-tooltip="Join call"],'
                ' [data-tooltip="Start call"],'
                ' .top-bar button[aria-label*="call" i]'
            )
            await join_btn.first.click(timeout=30000)
            log.info("Clicked initial Join call button")

            # Wait for media settings dialog to appear
            await asyncio.sleep(3)

            # Click the actual "Join call" button inside the dialog
            # (audio/video are already muted at the getUserMedia level)
            dialog_join = page.locator(
                '.media-settings button:has-text("Join call"),'
                ' .media-settings button:has-text("Start call"),'
                ' .modal-container button:has-text("Join call"),'
                ' .modal-container button:has-text("Start call"),'
                ' [class*="call-button"]:has-text("Join call"),'
                ' [class*="call-button"]:has-text("Start call")'
            )
            try:
                if await dialog_join.first.is_visible(timeout=5000):
                    await dialog_join.first.click()
                    log.info("Clicked dialog Join call button (media settings)")
                else:
                    log.info("No media settings dialog found, may have joined directly")
            except Exception:
                # Try clicking any remaining visible join button
                try:
                    all_join = page.locator(
                        'button:has-text("Join call"), button:has-text("Start call")'
                    )
                    count = await all_join.count()
                    log.info("Found %d join buttons after dialog check", count)
                    for i in range(count):
                        btn = all_join.nth(i)
                        if await btn.is_visible():
                            await btn.click()
                            log.info("Clicked visible join button #%d", i)
                            break
                except Exception:
                    log.info("No additional join buttons found")

            log.info("Join sequence complete for room %s", room_token)

            # Wait for WebRTC to connect — SFU via HPB can take 10-15s
            for wait_i in range(6):
                await asyncio.sleep(5)
                diag = await page.evaluate("""() => ({
                    hasRecorder: window.__notetaker_recorder !== null,
                    hasCtx: window.__notetaker_ctx !== null,
                    chunks: window.__notetaker_chunks.length,
                    pcCount: window.__notetaker_pc_count || 0,
                    trackCount: window.__notetaker_track_count || 0,
                    gumCount: window.__notetaker_gum_count || 0,
                })""")
                log.info("Audio capture check %d/6: %s", wait_i + 1, diag)
                if diag.get("hasRecorder"):
                    log.info("In-browser audio capture active")
                    break
            else:
                log.warning(
                    "No remote audio tracks after 30s. PCs: %d, tracks: %d, gUM: %d",
                    diag.get("pcCount", 0),
                    diag.get("trackCount", 0),
                    diag.get("gumCount", 0),
                )

            log.info("Recording in progress for room %s...", room_token)

            # Wait for call to end by polling the Talk API.
            # Flush audio chunks to disk every 5 minutes to bound memory
            # usage for long calls (2+ hours).
            # Grace period: require multiple consecutive "no participants"
            # checks before ending, to handle brief reconnection gaps.
            auth = (self.user, self.password)
            poll_count = 0
            total_audio_bytes = 0
            empty_streak = 0
            grace_checks = 6  # 6 × 10s = 60s grace period
            while True:
                await asyncio.sleep(10)
                poll_count += 1

                # Log diagnostics every 3rd poll (30s)
                if poll_count % 3 == 1:
                    diag = await page.evaluate("""() => ({
                        hasRecorder: window.__notetaker_recorder !== null,
                        chunks: window.__notetaker_chunks.length,
                        pcCount: window.__notetaker_pc_count || 0,
                        trackCount: window.__notetaker_track_count || 0,
                        ctxState: window.__notetaker_ctx ? window.__notetaker_ctx.state : 'none',
                    })""")
                    log.info("Audio status: %s", diag)
                else:
                    has_recorder = await page.evaluate(
                        "() => window.__notetaker_recorder !== null"
                    )
                    if not has_recorder:
                        log.debug("Still waiting for remote audio tracks...")

                # Flush audio chunks to disk every 5 minutes (30 polls × 10s)
                if poll_count % 30 == 0:
                    flushed_b64 = await page.evaluate(FLUSH_CHUNKS_JS)
                    if flushed_b64:
                        flushed_data = base64.b64decode(flushed_b64)
                        with open(output_path, "ab") as f:
                            f.write(flushed_data)
                        total_audio_bytes += len(flushed_data)
                        log.info(
                            "Flushed %d bytes to disk (total: %d bytes)",
                            len(flushed_data),
                            total_audio_bytes,
                        )

                if not _others_in_call(self.nextcloud_url, auth, room_token, self.user):
                    empty_streak += 1
                    if empty_streak == 1:
                        log.info(
                            "No other participants in room %s, starting grace period (%ds)",
                            room_token,
                            grace_checks * 10,
                        )
                    if empty_streak >= grace_checks:
                        log.info(
                            "Call ended in room %s (no participants for %ds)",
                            room_token,
                            empty_streak * 10,
                        )
                        break
                else:
                    if empty_streak > 0:
                        log.info(
                            "Participants returned to room %s after %ds empty, continuing",
                            room_token,
                            empty_streak * 10,
                        )
                    empty_streak = 0

            # Extract speaker timeline BEFORE closing the recorder/browser —
            # DOM and __nt_* state are still live here.
            speaker_events: list[dict] = []
            try:
                tl = await page.evaluate(EXTRACT_TIMELINE_JS)
                raw_events = tl.get("timeline", []) if tl else []
                labels = tl.get("labels", {}) if tl else {}
                log.info(
                    "Speaker timeline: %d events across %d streams; DOM labels: %s",
                    len(raw_events),
                    len(tl.get("streamIds", []) if tl else []),
                    {k: v for k, v in labels.items() if v},
                )
                # Fallback label assignment: if DOM scrape failed for a stream,
                # fetch participants from Talk API and match by enumeration order.
                unlabeled = [
                    sid
                    for sid in (tl.get("streamIds", []) if tl else [])
                    if not labels.get(sid)
                ]
                if unlabeled:
                    try:
                        resp = requests.get(
                            f"{self.nextcloud_url}/ocs/v2.php/apps/spreed/api/v4/room/{room_token}/participants",
                            auth=(self.user, self.password),
                            headers=OCS_HEADERS,
                            timeout=10,
                        )
                        resp.raise_for_status()
                        others = [
                            p.get("displayName") or p.get("actorId")
                            for p in resp.json()["ocs"]["data"]
                            if p.get("actorType") == "users"
                            and p.get("actorId") != self.user
                        ]
                        for i, sid in enumerate(unlabeled):
                            if i < len(others):
                                labels[sid] = others[i]
                        log.info("Fallback ordinal labels applied: %s", labels)
                    except Exception:
                        log.warning("Ordinal label fallback failed", exc_info=True)

                # Build speaker_events with resolved labels; unknowns -> Speaker N
                sid_to_speaker: dict[str, str] = {}
                speaker_counter = 0
                for ev in raw_events:
                    sid = ev.get("streamId")
                    label = labels.get(sid)
                    if not label:
                        if sid not in sid_to_speaker:
                            speaker_counter += 1
                            sid_to_speaker[sid] = f"Speaker {speaker_counter}"
                        label = sid_to_speaker[sid]
                    speaker_events.append(
                        {
                            "start_ms": int(ev.get("startMs", 0)),
                            "end_ms": int(ev.get("endMs", 0)),
                            "label": label,
                        }
                    )
            except Exception:
                log.exception("Failed to extract speaker timeline")

            # Extract remaining recorded audio from the browser
            log.info("Extracting remaining audio from browser...")
            audio_b64 = await page.evaluate(EXTRACT_AUDIO_JS)
            if audio_b64:
                audio_data = base64.b64decode(audio_b64)
                with open(output_path, "ab") as f:
                    f.write(audio_data)
                total_audio_bytes += len(audio_data)

            await browser.close()

        if total_audio_bytes > 0:
            log.info(
                "Audio saved to %s (%d bytes, %.1f KB)",
                output_path,
                total_audio_bytes,
                total_audio_bytes / 1024,
            )
        else:
            log.warning("No audio captured -- recording may be empty")
            # Write empty file so downstream doesn't crash
            with open(output_path, "wb") as f:
                pass

        return output_path, speaker_events
