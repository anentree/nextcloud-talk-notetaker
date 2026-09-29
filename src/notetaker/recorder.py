from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
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
    window.__nt_timeline = [];   // {trackKey, startMs, endMs}
    window.__nt_t0 = null;
    window.__nt_peers = {};      // trackKey -> {analyser, active, startedAt, belowSince}
    // Map trackKey -> the actual MediaStreamTrack object, and record every
    // (element, tracks) binding. DOM name lookup matches by track *identity*
    // rather than by track.id, because Janus reuses ids like "janus0" across
    // every subscriber peer connection.
    window.__nt_keyToTrack = {};
    window.__nt_elBindings = [];  // [{el, tracks}] most recent last
    window.__nt_wsUrls = [];      // every WebSocket URL the page opened
    window.__nt_vadInterval = null;
    // VAD_THRESHOLD is templated from Python (env-configurable).
    const VAD_THRESHOLD = __VAD_THRESHOLD__;
    const VAD_HANGOVER_MS = 400; // silence duration that ends an utterance
    const AUDIBLE_RMS = 1 / 128;  // one step of getByteTimeDomainData
    // Per-peer RMS stats for tuning visibility
    window.__nt_rms_stats = {}; // trackKey -> {sum, peak, count}

    // === SIGNALING-BASED SPEAKER IDENTITY ===
    // Maps built from intercepted HPB WebSocket messages.
    // These provide authoritative track→participant mapping without DOM scraping.
    window.__nt_sigMap = {};        // sessionId -> {userId, displayName}
    window.__nt_sdpToSession = {};  // sdpFingerprint -> sessionId
    window.__nt_trackToSession = {}; // trackKey -> sessionId

    function processSignalingMessage(rawData) {
        let msg;
        try { msg = JSON.parse(rawData); } catch(e) { return; }

        // Room join events: sessionId -> userId + displayName
        if (msg.type === 'event' && msg.event) {
            if (msg.event.target === 'room' && msg.event.type === 'join' && msg.event.join) {
                for (const user of msg.event.join) {
                    if (user.sessionid) {
                        const displayName = (user.user && (user.user.displayname || user.user.displayName))
                            || user.userid || user.sessionid;
                        window.__nt_sigMap[user.sessionid] = {
                            userId: user.userid || null,
                            displayName: displayName,
                        };
                        console.log('[notetaker] Signaling join: session=' + user.sessionid +
                            ' userId=' + user.userid + ' name=' + displayName);
                    }
                }
            }
            // Participants update: may carry userId for sessions we've seen
            if (msg.event.target === 'participants' && msg.event.update) {
                const users = msg.event.update.users || msg.event.update.changed || [];
                if (Array.isArray(users)) for (const user of users) {
                    const sid = user.sessionId || user.sessionid;
                    const uid = user.userId || user.userid;
                    if (sid && uid && !window.__nt_sigMap[sid]) {
                        window.__nt_sigMap[sid] = {
                            userId: uid,
                            displayName: uid,
                        };
                        console.log('[notetaker] Signaling participant: session=' + sid + ' userId=' + uid);
                    }
                }
            }
        }

        // Offer messages: extract sender sessionId + SDP fingerprint
        if (msg.type === 'message' && msg.message && msg.message.sender) {
            const senderSession = msg.message.sender.sessionid;
            if (senderSession) {
                try {
                    const data = typeof msg.message.data === 'string'
                        ? JSON.parse(msg.message.data) : msg.message.data;
                    if (data.type === 'offer' && data.payload && data.payload.sdp) {
                        const fp = data.payload.sdp.substring(0, 200);
                        // Cap map size to prevent unbounded growth from renegotiations
                        const keys = Object.keys(window.__nt_sdpToSession);
                        if (keys.length > 50) delete window.__nt_sdpToSession[keys[0]];
                        window.__nt_sdpToSession[fp] = senderSession;
                        console.log('[notetaker] Signaling offer: session=' + senderSession +
                            ' sdpFp=' + fp.substring(0, 40) + '...');
                    }
                } catch(e) {}
            }
        }
    }

    // Intercept WebSocket construction to capture HPB signaling messages.
    // Talk creates a WebSocket to the signaling server; we add a passive
    // listener for incoming messages to extract participant identity.
    const OrigWebSocket = window.WebSocket;
    window.WebSocket = new Proxy(OrigWebSocket, {
        construct(target, args) {
            const ws = Reflect.construct(target, args);
            const url = (args[0] || '').toString();
            window.__nt_wsUrls.push(url);
            // Match the standalone (HPB) signaling endpoint. Talk builds it as
            // <signaling_server>/spreed, so a deployment configured with
            // ".../standalone-signaling" yields ".../standalone-signaling/spreed"
            // — which does NOT contain the substring "/signaling". Matching on
            // that substring alone silently disabled signaling attribution.
            const isSignaling = /^wss?:/i.test(url) &&
                (/signaling/i.test(url) || /\\/spreed(\\/|\\?|$)/i.test(url));
            if (isSignaling) {
                console.log('[notetaker] Intercepting signaling WebSocket: ' + url);
                ws.addEventListener('message', function(event) {
                    try { processSignalingMessage(event.data); } catch(e) {}
                });
            } else {
                console.log('[notetaker] Ignoring non-signaling WebSocket: ' + url);
            }
            return ws;
        }
    });

    function startVadLoop() {
        if (window.__nt_vadInterval) return;
        window.__nt_vadInterval = setInterval(() => {
            const now = performance.now();
            if (window.__nt_t0 === null) window.__nt_t0 = now;
            for (const key of Object.keys(window.__nt_peers)) {
                const peer = window.__nt_peers[key];
                if (!peer.buf) peer.buf = new Uint8Array(peer.analyser.fftSize);
                peer.analyser.getByteTimeDomainData(peer.buf);
                let sumSq = 0;
                for (let i = 0; i < peer.buf.length; i++) {
                    const v = (peer.buf[i] - 128) / 128;
                    sumSq += v * v;
                }
                const rms = Math.sqrt(sumSq / peer.buf.length);
                // Track RMS stats for tuning visibility
                let stats = window.__nt_rms_stats[key];
                if (!stats) {
                    stats = { sum: 0, peak: 0, count: 0 };
                    window.__nt_rms_stats[key] = stats;
                }
                stats.sum += rms;
                if (rms > stats.peak) stats.peak = rms;
                stats.count += 1;
                const speaking = rms > VAD_THRESHOLD;
                // The silence alert asks "is this track alive", not "is
                // someone talking": one 8-bit step of signal is enough. A
                // dead subscription reads exactly zero; a quiet speaker
                // rarely reaches VAD_THRESHOLD but is never at zero.
                if (rms >= AUDIBLE_RMS) window.__nt_lastVoiceAt = now;
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
                            trackKey: key,
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

    // Hook HTMLMediaElement.srcObject so we can map a track back to its <video>.
    // Talk's UI assigns remote streams to a <video> for rendering, and the
    // surrounding tile carries the participant's display name.
    //
    // We record the track OBJECTS, not their ids. Neither stream.id nor
    // track.id is unique under an HPB/Janus SFU — every subscriber peer
    // connection produces "janus0"/"janus1" — so an id-keyed map is
    // last-writer-wins and collapses every participant onto one element.
    try {
        const desc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'srcObject');
        if (desc && desc.set) {
            Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', {
                configurable: true,
                get: desc.get,
                set(stream) {
                    desc.set.call(this, stream);
                    try {
                        if (stream) {
                            window.__nt_elBindings.push({
                                el: this,
                                tracks: stream.getTracks(),
                            });
                            // Bound growth across long calls with many re-mounts.
                            if (window.__nt_elBindings.length > 200) {
                                window.__nt_elBindings.shift();
                            }
                        }
                    } catch(e) {}
                }
            });
        }
    } catch(e) {
        console.error('[notetaker] srcObject hook failed:', e);
    }

    function startRecordingAudioTrack(track, trackKey) {
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
            console.log('[notetaker] Connected audio track to recording destination (trackKey=' + trackKey + ')');

            // Per-track analyser for the speaker timeline. trackKey is
            // namespaced by peer connection (see hookPC) because Janus reuses
            // track ids across PCs — an unnamespaced key made this guard drop
            // every participant after the first.
            if (trackKey && !window.__nt_peers[trackKey]) {
                const analyser = window.__notetaker_ctx.createAnalyser();
                analyser.fftSize = 512;
                analyser.smoothingTimeConstant = 0.2;
                source.connect(analyser);
                window.__nt_peers[trackKey] = {
                    analyser, active: false, startedAt: 0, belowSince: null,
                };
                startVadLoop();
                console.log('[notetaker] VAD analyser attached for track ' + trackKey);
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
                // Anchor speaker timeline t=0 to the moment audio recording starts
                // (otherwise the timeline drifts ~100ms behind the audio file).
                window.__nt_t0 = performance.now();
                console.log('[notetaker] MediaRecorder started (' + mimeType + '), t0=' + window.__nt_t0);
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
        // Stable per-PC id. Track keys are namespaced with it so that two
        // subscriber connections both reporting track.id "janus0" produce
        // distinct keys ("1:janus0", "2:janus0") instead of colliding.
        pc.__nt_pcId = window.__notetaker_pc_count;
        (window.__nt_pcs = window.__nt_pcs || []).push(pc);
        console.log('[notetaker] Hooked PeerConnection #' + pc.__nt_pcId +
            ' signalingState=' + pc.signalingState);

        pc.addEventListener('track', (event) => {
            window.__notetaker_track_count++;
            const streamId = (event.streams && event.streams[0]) ? event.streams[0].id : null;
            const rawId = event.track.id || streamId || 'a';
            const trackKey = pc.__nt_pcId + ':' + rawId;
            console.log('[notetaker] Track event: kind=' + event.track.kind +
                ' readyState=' + event.track.readyState +
                ' trackId=' + event.track.id +
                ' streamId=' + streamId +
                ' trackKey=' + trackKey +
                ' pcSession=' + (pc.__nt_sessionId || 'none') +
                ' (#' + window.__notetaker_track_count + ')');
            if (event.track.kind === 'audio') {
                // Propagate signaling identity to track level
                if (pc.__nt_sessionId) {
                    window.__nt_trackToSession[trackKey] = pc.__nt_sessionId;
                }
                window.__nt_keyToTrack[trackKey] = event.track;
                startRecordingAudioTrack(event.track, trackKey);
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

        // Correlate this PC with the signaling session that sent the offer.
        // The SDP content is identical in the WebSocket message and here.
        if (desc && desc.type === 'offer' && desc.sdp && !this.__nt_sessionId) {
            const fp = desc.sdp.substring(0, 200);
            const sessionId = window.__nt_sdpToSession[fp];
            if (sessionId) {
                this.__nt_sessionId = sessionId;
                const info = window.__nt_sigMap[sessionId] || {};
                console.log('[notetaker] PC→session: ' + sessionId +
                    ' (' + (info.displayName || 'unknown') + ')');
                delete window.__nt_sdpToSession[fp];
            }
        }

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

# Polled every 10s during a call. liveAudio counts remote audio tracks that
# are live and not muted on connections that are still up; the bot's own
# publisher connection has only muted receivers, so it does not count.
AUDIO_STATUS_JS = """() => ({
    hasRecorder: window.__notetaker_recorder !== null,
    chunks: window.__notetaker_chunks.length,
    pcCount: window.__notetaker_pc_count || 0,
    trackCount: window.__notetaker_track_count || 0,
    ctxState: window.__notetaker_ctx ? window.__notetaker_ctx.state : 'none',
    liveAudio: (window.__nt_pcs || [])
        .filter(pc => pc.connectionState !== 'closed' && pc.connectionState !== 'failed')
        .reduce((n, pc) => n + pc.getReceivers().filter(r => r.track &&
            r.track.kind === 'audio' && r.track.readyState === 'live' &&
            !r.track.muted).length, 0),
    voiceAgoS: window.__nt_lastVoiceAt
        ? (performance.now() - window.__nt_lastVoiceAt) / 1000 : null,
})"""

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
    // Stop the VAD loop so it doesn't race with extraction
    if (window.__nt_vadInterval) {
        clearInterval(window.__nt_vadInterval);
        window.__nt_vadInterval = null;
    }
    // Flush any in-progress utterances
    const now = performance.now();
    for (const key of Object.keys(window.__nt_peers || {})) {
        const peer = window.__nt_peers[key];
        if (peer.active && window.__nt_t0 !== null) {
            window.__nt_timeline.push({
                trackKey: key,
                startMs: Math.round(peer.startedAt - window.__nt_t0),
                endMs: Math.round(now - window.__nt_t0),
            });
            peer.active = false;
        }
    }

    // === LABEL RESOLUTION (signaling > DOM) ===

    // Primary: signaling-derived labels (authoritative)
    const sigLabels = {};
    let sigCount = 0;
    for (const trackKey of Object.keys(window.__nt_trackToSession || {})) {
        const sessionId = window.__nt_trackToSession[trackKey];
        const info = window.__nt_sigMap[sessionId];
        if (info && info.displayName) {
            sigLabels[trackKey] = info.displayName;
            sigCount++;
        }
    }

    // Secondary: DOM-derived labels (fallback for tracks without signaling data)
    //
    // The name must come from the tile that actually renders THIS track. The
    // previous implementation climbed up to 10 ancestors calling querySelector
    // at each level; once the climb reached the video grid, querySelector
    // returned the first name in the whole grid, so every track got the same
    // name. We now resolve the owning tile and refuse to look outside it.
    const NAME_SELECTOR = '.nameIndicator, .participant-name, [class*="nameIndicator"], [class*="participantName"], [data-participant-name]';
    const TILE_SELECTOR = '[class*="videoContainer"], [class*="videoWrapper"], [class*="participant"], [class*="Participant"]';

    // Count NAME elements, not <video> elements, to decide whether a node is
    // a single participant tile. Cameras are usually off on these calls, so
    // Talk binds streams to <audio> and a video-based guard never trips —
    // which reproduced the original "one name for every track" bug.
    function spansMultipleParticipants(node) {
        if (!node || !node.querySelectorAll) return false;
        const names = node.querySelectorAll(NAME_SELECTOR).length;
        if (names > 1) return true;
        // Media count is only a fallback signal: a single tile legitimately
        // holds both a <video> and an <audio> element, so counting media
        // alone would reject valid tiles.
        if (names === 0 && node.querySelectorAll('video, audio').length > 1) {
            return true;
        }
        return false;
    }

    function tileFor(el) {
        // Prefer an explicit tile ancestor — but only if it really is one
        // tile. TILE_SELECTOR can also match the grid container (e.g.
        // "participantsGrid"), which would hand back the wrong name or none.
        if (el.closest) {
            const t = el.closest(TILE_SELECTOR);
            if (t && !spansMultipleParticipants(t)) return t;
        }
        // Otherwise climb, but stop before entering a container that spans
        // more than one participant — that is the grid, not a tile.
        let node = el.parentElement;
        let best = null;
        for (let d = 0; d < 5 && node; d++) {
            if (spansMultipleParticipants(node)) break;
            best = node;
            node = node.parentElement;
        }
        return best;
    }

    function nameForElement(el) {
        const tile = tileFor(el);
        if (!tile || !tile.querySelector) return null;
        // Guard against a container that spans several participants.
        if (spansMultipleParticipants(tile)) return null;
        const cand = tile.querySelector(NAME_SELECTOR);
        if (cand && cand.textContent && cand.textContent.trim()) {
            return cand.textContent.trim();
        }
        const dn = tile.querySelector('[data-display-name]');
        const dnv = dn && dn.getAttribute('data-display-name');
        return dnv || null;
    }

    const domLabels = {};
    let domCount = 0;
    const bindings = window.__nt_elBindings || [];
    for (const trackKey of Object.keys(window.__nt_keyToTrack || {})) {
        const track = window.__nt_keyToTrack[trackKey];
        if (!track) continue;
        // Most recent binding containing this exact track object wins.
        let el = null;
        for (let i = bindings.length - 1; i >= 0; i--) {
            if (bindings[i].tracks && bindings[i].tracks.indexOf(track) !== -1) {
                el = bindings[i].el;
                break;
            }
        }
        if (!el) continue;
        const name = nameForElement(el);
        if (name) {
            domLabels[trackKey] = name;
            domCount++;
        }
    }

    // Merge: signaling wins, DOM fills gaps
    const labels = {};
    for (const trackKey of Object.keys(window.__nt_peers || {})) {
        labels[trackKey] = sigLabels[trackKey] || domLabels[trackKey] || null;
    }

    console.log('[notetaker] Label resolution: signaling=' + sigCount +
        ' dom=' + domCount + ' sigMap=' + JSON.stringify(window.__nt_sigMap));

    // Per-peer RMS summary for threshold tuning visibility
    const rmsSummary = {};
    for (const k of Object.keys(window.__nt_rms_stats || {})) {
        const s = window.__nt_rms_stats[k];
        rmsSummary[k] = {
            mean: s.count > 0 ? s.sum / s.count : 0,
            peak: s.peak,
            samples: s.count,
        };
    }
    return {
        timeline: window.__nt_timeline || [],
        labels: labels,
        trackKeys: Object.keys(window.__nt_peers || {}),
        rmsSummary: rmsSummary,
        sigLabels: sigLabels,
        domLabels: domLabels,
        sigMap: window.__nt_sigMap || {},
        wsUrls: window.__nt_wsUrls || [],
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


def resolve_stream_labels(
    stream_ids: list[str],
    known_labels: dict[str, str | None],
    other_participants: list[str],
    authoritative_keys: set[str] | None = None,
) -> tuple[dict[str, str], int, int, int, int]:
    """Resolve track/stream IDs to display-name labels.

    Returns (labels, known_count, ordinal_count, speaker_n_count, conflicts).

    ``known_labels`` may contain labels from any source (signaling,
    DOM scraping, etc.) — callers merge upstream before calling.

    Rules:
    - Known labels win (any non-None value)...
    - ...unless the same name was claimed by more than one track AND the
      claim did not come from signaling. Guessed labels colliding means the
      labelling source is wrong (two tracks cannot be the same person), so
      every track involved is demoted to Speaker N. A confidently wrong name
      is worse than an honest anonymous one: the transcriber prompt treats
      the timeline as authoritative and will override its own voice-based
      diarization with it.
    - ``authoritative_keys`` are track keys whose label came from the
      signaling server, which reports true identity. One person can
      legitimately own several tracks there (a reconnect or renegotiation
      creates a new peer connection), so those duplicates are kept.
    - Ordinal fallback is only applied when EXACTLY one ID is unlabeled
      and exactly one participant name remains unclaimed (the unambiguous
      case). Otherwise unlabeled IDs become Speaker N — we never guess
      among multiple plausible names.
    """
    labels: dict[str, str] = {}
    for sid in stream_ids:
        v = known_labels.get(sid)
        if v:
            labels[sid] = v

    # Demote any name claimed by more than one track, unless every claim to
    # that name came from signaling (where one person really can own several
    # tracks after a reconnect).
    authoritative = authoritative_keys or set()
    counts: dict[str, int] = {}
    for name in labels.values():
        counts[name] = counts.get(name, 0) + 1

    trusted_names = {
        name
        for name in counts
        if all(
            sid in authoritative for sid, lbl in labels.items() if lbl == name
        )
    }

    conflicts = 0

    # Mixed case: signaling says track A is Piyush and DOM guesses the same
    # name for track B. Signaling is authoritative everywhere else, so keep
    # its claim and drop only the guess rather than losing both.
    for name, count in list(counts.items()):
        if count <= 1 or name in trusted_names:
            continue
        claimants = [sid for sid, lbl in labels.items() if lbl == name]
        signalled = [sid for sid in claimants if sid in authoritative]
        if len(signalled) == 1:
            for sid in claimants:
                if sid not in authoritative:
                    del labels[sid]
                    conflicts += 1
            counts[name] = 1
            trusted_names.add(name)
            log.info(
                "Name %r was claimed by signaling for track %s and guessed for "
                "%d other track(s); keeping the signaling claim",
                name,
                signalled[0],
                len(claimants) - 1,
            )

    for sid in list(labels):
        name = labels[sid]
        if counts.get(name, 0) > 1 and name not in trusted_names:
            log.warning(
                "Track %s and %d other track(s) both resolved to %r without "
                "signaling confirmation — demoting all of them to anonymous "
                "speakers rather than risk misattributing words",
                sid,
                counts[name] - 1,
                name,
            )
            del labels[sid]
            conflicts += 1
        elif counts.get(name, 0) > 1:
            log.info(
                "Track %s shares the signaling-confirmed name %r with %d other "
                "track(s) — same person across reconnects, keeping the label",
                sid,
                name,
                counts[name] - 1,
            )

    known_count = len(labels)

    unlabeled = [sid for sid in stream_ids if sid not in labels]
    claimed = set(labels.values())
    remaining = [n for n in other_participants if n and n not in claimed]
    ordinal_count = 0
    if len(unlabeled) == 1 and len(remaining) == 1:
        labels[unlabeled[0]] = remaining[0]
        ordinal_count = 1

    speaker_n = 0
    counter = 0
    for sid in stream_ids:
        if sid not in labels:
            counter += 1
            labels[sid] = f"Speaker {counter}"
            speaker_n += 1
    return labels, known_count, ordinal_count, speaker_n, conflicts


def _call_presence(
    base_url: str, auth: tuple, room_token: str, own_user: str
) -> tuple[set[str], int, bool] | None:
    """Return (registered user IDs in the call, total others in the call,
    whether the bot itself is in the call).

    Returns None if the check itself failed, which callers must treat as
    "unknown", not as "nobody is here".

    The two values are deliberately different populations:

    - The count includes guests and federated participants, because whether
      the call is still running has nothing to do with whether an attendee
      has a Nextcloud account. Filtering it to registered users would end a
      recording while guests were still talking.
    - The ID set contains only registered users, because that is who we can
      resolve to an email address.

    This is the only moment attendance is observable. Once a call ends every
    participant's ``inCall`` flag resets to 0, so who was actually present
    cannot be recovered afterwards — which is why the caller accumulates the
    union of these sets across the whole call.

    The bot's own flag is how the caller notices it was dropped from a call
    that is still running (2026-09-29: an SFU teardown when one participant
    lost connection took the bot out of the call, and it recorded 77 minutes
    of silence while the others talked).
    """
    try:
        resp = requests.get(
            f"{base_url}/ocs/v2.php/apps/spreed/api/v4/room/{room_token}/participants",
            auth=auth,
            headers=OCS_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        ids: set[str] = set()
        total = 0
        self_in_call = False
        for p in resp.json()["ocs"]["data"]:
            actor_id = p.get("actorId")
            if actor_id == own_user:
                self_in_call = self_in_call or p.get("inCall", 0) > 0
                continue
            if not p.get("inCall", 0) > 0:
                continue
            total += 1
            # actorType is absent on older Talk API responses; treat a missing
            # value as a registered user rather than dropping the attendee.
            if p.get("actorType", "users") == "users" and actor_id:
                ids.add(actor_id)
        return ids, total, self_in_call
    except Exception:
        log.warning("Failed to check call participants via API, assuming still active")
        return None


def _others_in_call(base_url: str, auth: tuple, room_token: str, own_user: str) -> bool:
    """Check via Talk API if any other participants are still in the call."""
    presence = _call_presence(base_url, auth, room_token, own_user)
    return True if presence is None else presence[1] > 0


def _log_alert_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("Silence alert failed: %s", task.exception())


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

    # Set by the caller so the recorder can prove liveness during a long call.
    heartbeat_fn = None
    # Set by the caller: alert_fn(silent_seconds, others_in_call, rejoins) is
    # called once per stretch of silence_alert_s with nobody heard while
    # others are in the call. 0 disables it.
    alert_fn = None
    silence_alert_s = 0
    # Seconds the bot was in the call, set when the call ends.
    in_call_s = None

    async def _join_call(self, page, rejoin: bool = False) -> None:
        """Click through Talk's join flow. Used for the first join and to
        rejoin a call the bot was dropped from; raises if no join button
        appears.

        A rejoin only ever clicks "Join call": the call is known to be
        running, so "Start call" could only appear in a race with the call
        ending, and clicking it would start a new call that rings the room.
        """
        # Click the actual "Join call" button inside the dialog
        # (audio/video are already muted at the getUserMedia level)
        dialog_texts = ("Join call",) if rejoin else ("Join call", "Start call")
        dialog_selectors = [
            f'{scope} button:has-text("{text}")'
            for scope in (".media-settings", ".modal-container")
            for text in dialog_texts
        ] + [f'[class*="call-button"]:has-text("{text}")' for text in dialog_texts]
        dialog_join = page.locator(",".join(dialog_selectors))
        # Only a visible dialog: a hidden modal earlier in the DOM would be
        # .first and hide a real one.
        # Playwright splits on ">>" before ",", so the filter goes once, after
        # the whole list; per-alternative filters can never match.
        open_dialog = page.locator(",".join(dialog_selectors) + " >> visible=true")
        if rejoin and await open_dialog.first.is_visible():
            # A previous attempt opened the media-settings dialog and missed
            # it. The top-bar button is under its overlay now, so clicking
            # that would time out on every attempt for the rest of the call.
            await open_dialog.first.click()
            log.info("Clicked the Join call button of an already open dialog")
            return

        # Join the call — Talk has a two-step process:
        # 1. Click "Join call" in the top bar → opens media settings dialog
        # 2. Click "Join call" inside the dialog → actually joins the call
        if rejoin:
            selector = 'button:has-text("Join call"), [data-tooltip="Join call"]'
        else:
            selector = (
                'button:has-text("Join call"),'
                ' button:has-text("Start call"),'
                ' [data-tooltip="Join call"],'
                ' [data-tooltip="Start call"],'
                # Catch-all for a relabelled button. Never on a rejoin: if
                # the bot is in fact still in the call, this matches "Leave
                # call".
                ' .top-bar button[aria-label*="call" i]'
            )
        join_btn = page.locator(selector)
        await join_btn.first.click(timeout=30000)
        log.info("Clicked initial Join call button")

        # Wait for media settings dialog to appear
        await asyncio.sleep(3)

        try:
            # is_visible() ignores its timeout and answers immediately; a
            # dialog that rendered a moment late was reported as "joined
            # directly" and left open. wait_for really waits.
            await dialog_join.first.wait_for(state="visible", timeout=5000)
            await dialog_join.first.click()
            log.info("Clicked dialog Join call button (media settings)")
        except Exception:
            log.info("No media settings dialog found, may have joined directly")
            # Try clicking any remaining visible join button
            try:
                all_join = page.locator(
                    ", ".join(f'button:has-text("{text}")' for text in dialog_texts)
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

    async def _call_button_labels(self, page) -> list[str]:
        """Labels of the call buttons on the page, for a failed rejoin's log."""
        try:
            return await page.evaluate(
                """() => Array.from(document.querySelectorAll('button'))
                    .map(b => (b.getAttribute('aria-label') || b.textContent || '').trim())
                    .filter(t => /call/i.test(t)).slice(0, 10)"""
            )
        except Exception:
            return []

    async def record_call(
        self, room_token: str, conversation_name: str
    ) -> tuple[str, list[dict], set[str]]:
        """Join a Talk call, record audio, return path to audio file.

        Blocks until the call ends (all other participants leave).
        Uses in-browser MediaRecorder to capture WebRTC audio directly.
        """
        output_path = self._output_path(room_token, conversation_name)

        # Audio is appended in 5-minute flushes. The filename is derived from
        # the date and room, so a previous call in the same room on the same
        # day that crashed mid-recording would leave a file here and this
        # call would append to it — splicing two meetings into one recording.
        # Start from a clean file every time.
        if os.path.exists(output_path):
            log.warning(
                "Removing leftover recording from an earlier run: %s (%d bytes)",
                output_path,
                os.path.getsize(output_path),
            )
            try:
                os.unlink(output_path)
            except OSError:
                log.exception("Could not remove leftover recording %s", output_path)

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
            # BEFORE Talk's JavaScript creates RTCPeerConnections.
            # VAD threshold is env-configurable for tuning per deployment.
            vad_threshold = float(os.environ.get("NOTETAKER_VAD_THRESHOLD", "0.02"))
            init_js = AUDIO_CAPTURE_INIT_JS.replace(
                "__VAD_THRESHOLD__", repr(vad_threshold)
            )
            await context.add_init_script(init_js)

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

            await self._join_call(page)
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
            # Union of everyone seen in the call across its whole lifetime.
            # Someone who joined for five minutes and left still attended.
            attendee_ids: set[str] = set()
            # Consecutive polls with others in the call and the bot not.
            # Two in a row (20s) is a drop; one can be the flag lagging.
            dropped_streak = 0
            rejoins = 0
            silent_streak = 0
            joined_at = time.monotonic()
            silence_alerted = False
            alert_tasks: set[asyncio.Task] = set()
            while True:
                await asyncio.sleep(10)
                poll_count += 1

                # Prove liveness while a call is in progress. The outer poll
                # loop is blocked for the whole call, so without this the
                # container reports unhealthy after a few minutes of every
                # real meeting — the exact inverse of the intended signal.
                if self.heartbeat_fn is not None:
                    try:
                        self.heartbeat_fn()
                    except Exception:
                        log.debug("Heartbeat callback failed", exc_info=True)

                diag = await page.evaluate(AUDIO_STATUS_JS)
                diag = diag if isinstance(diag, dict) else {}
                # Log diagnostics every 3rd poll (30s)
                if poll_count % 3 == 1:
                    log.info("Audio status: %s", diag)
                elif not diag.get("hasRecorder"):
                    log.debug("Still waiting for remote audio tracks...")
                live_audio = diag.get("liveAudio")

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

                presence = _call_presence(
                    self.nextcloud_url, auth, room_token, self.user
                )
                if presence is not None:
                    current_ids, others_present, self_in_call = presence
                    new_ids = current_ids - attendee_ids
                    if new_ids:
                        log.info("Attendees joined the call: %s", sorted(new_ids))
                    attendee_ids |= current_ids
                else:
                    # The check failed — treat as "still active" rather than
                    # ending the recording on a transient API error.
                    others_present = 1
                    self_in_call = None

                if self_in_call is None:
                    # Unknown: neither evidence of a drop nor of being back.
                    pass
                elif others_present and not self_in_call:
                    dropped_streak += 1
                    if dropped_streak >= 2:
                        dropped_streak = 0
                        rejoins += 1
                        log.warning(
                            "Bot is no longer in the call in room %s while %d "
                            "other(s) are; rejoining (rejoin #%d)",
                            room_token,
                            others_present,
                            rejoins,
                        )
                        try:
                            await self._join_call(page, rejoin=True)
                        except Exception as exc:  # noqa: BLE001 — keep recording
                            log.error(
                                "Rejoin #%d failed in room %s: %s: %s "
                                "(call buttons on the page: %s)",
                                rejoins,
                                room_token,
                                type(exc).__name__,
                                exc,
                                await self._call_button_labels(page),
                            )
                else:
                    dropped_streak = 0

                # Talk can still list the bot as in the call while its media
                # is dead. Only reported, not acted on: whether Chrome mutes a
                # remote track reliably when its RTP stops is unproven, and a
                # false alarm would make the bot leave and rejoin mid-call.
                # verify.py holds a recording that came out silent.
                # Nobody heard for a long stretch while people are in the
                # call: tell a human NOW, while the call can still be saved.
                # On 2026-09-29 the first sign of a dead recording was notes
                # about nothing, 78 minutes after it could have been fixed.
                voice_ago = diag.get("voiceAgoS")
                if voice_ago is None:
                    voice_ago = time.monotonic() - joined_at
                if (
                    self.silence_alert_s
                    and others_present
                    and voice_ago >= self.silence_alert_s
                ):
                    if not silence_alerted and self.alert_fn is not None:
                        silence_alerted = True
                        log.warning(
                            "No audio heard for %.0f min in room %s while %d "
                            "other(s) are in the call; alerting",
                            voice_ago / 60,
                            room_token,
                            others_present,
                        )
                        # In the background: a slow or down mail server must
                        # not stop presence polls, rejoins and flushes.
                        task = asyncio.create_task(
                            asyncio.to_thread(
                                self.alert_fn, voice_ago, others_present, rejoins
                            )
                        )
                        alert_tasks.add(task)
                        task.add_done_callback(_log_alert_failure)
                elif voice_ago < self.silence_alert_s:
                    # Heard again: a later silence deserves its own alert.
                    silence_alerted = False

                if others_present and self_in_call and live_audio == 0:
                    silent_streak += 1
                    if silent_streak == 6:
                        log.warning(
                            "No live remote audio for 60s in room %s while %d "
                            "other(s) are in the call (status %s)",
                            room_token,
                            others_present,
                            diag,
                        )
                else:
                    silent_streak = 0

                if not others_present:
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
                        # Time actually in the call, without the join and the
                        # grace period: the fallback length for verification.
                        self.in_call_s = max(
                            0.0, time.monotonic() - joined_at - empty_streak * 10
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

            if alert_tasks:
                # Let an alert still being sent finish, but never hold up the
                # notes for long; a spooled alert retries on its own.
                await asyncio.wait(alert_tasks, timeout=120)

            # Extract speaker timeline BEFORE closing the recorder/browser —
            # DOM and __nt_* state are still live here.
            speaker_events: list[dict] = []
            try:
                tl = await page.evaluate(EXTRACT_TIMELINE_JS)
                raw_events = tl.get("timeline", []) if tl else []
                labels = dict(tl.get("labels", {}) if tl else {})
                track_keys = list(tl.get("trackKeys", []) if tl else [])
                rms_summary = tl.get("rmsSummary", {}) if tl else {}
                # Diagnostic only — labels dict already has signaling > DOM merge
                sig_labels = tl.get("sigLabels", {}) if tl else {}
                dom_labels_js = tl.get("domLabels", {}) if tl else {}
                sig_map = tl.get("sigMap", {}) if tl else {}
                log.info(
                    "Speaker timeline: %d events across %d tracks",
                    len(raw_events),
                    len(track_keys),
                )
                log.info(
                    "Signaling labels (%d): %s",
                    len(sig_labels),
                    sig_labels,
                )
                log.info(
                    "DOM labels (%d): %s",
                    len({k: v for k, v in (dom_labels_js or {}).items() if v}),
                    {k: v for k, v in (dom_labels_js or {}).items() if v},
                )
                log.info(
                    "Signaling session map: %s",
                    sig_map,
                )
                # Logged at INFO deliberately: a signaling URL that no longer
                # matches the interception filter is invisible otherwise, and
                # that is exactly how attribution broke silently.
                ws_urls = tl.get("wsUrls", []) if tl else []
                log.info("WebSocket URLs observed (%d): %s", len(ws_urls), ws_urls)
                if not sig_map:
                    log.warning(
                        "Signaling map is EMPTY — speaker identity fell back to DOM "
                        "scraping. Check that one of the URLs above is the Talk "
                        "signaling endpoint and matches the interception filter."
                    )
                log.info(
                    "Merged labels: %s",
                    {k: v for k, v in labels.items() if v},
                )
                if rms_summary:
                    log.info(
                        "Per-track RMS (threshold=%s): %s",
                        vad_threshold,
                        {
                            k: f"mean={v.get('mean', 0):.4f} peak={v.get('peak', 0):.4f} n={v.get('samples', 0)}"
                            for k, v in rms_summary.items()
                        },
                    )
                # Always fetch participants to resolve userIds → displayNames
                # and to fill gaps in signaling data.
                others: list[str] = []
                try:
                    resp = requests.get(
                        f"{self.nextcloud_url}/ocs/v2.php/apps/spreed/api/v4/room/{room_token}/participants",
                        auth=(self.user, self.password),
                        headers=OCS_HEADERS,
                        timeout=10,
                    )
                    resp.raise_for_status()
                    api_participants = resp.json()["ocs"]["data"]
                    # Build userId → displayName lookup
                    uid_to_name = {}
                    for p in api_participants:
                        if (
                            p.get("actorType") == "users"
                            and p.get("actorId") != self.user
                        ):
                            uid = p.get("actorId", "")
                            dn = p.get("displayName") or uid
                            uid_to_name[uid] = dn
                            # `others` seeds the ordinal-name fallback, which
                            # writes a real name into the timeline the
                            # transcriber treats as ground truth. Only people
                            # actually on the call may enter that pool —
                            # otherwise a room member who never joined can be
                            # named as a speaker.
                            if not attendee_ids or uid in attendee_ids:
                                others.append(dn)
                    # Upgrade signaling labels: if a label matches a userId,
                    # replace it with the proper displayName.
                    for tk, name in list(labels.items()):
                        if name and name in uid_to_name and name != uid_to_name[name]:
                            labels[tk] = uid_to_name[name]
                            log.info(
                                "Upgraded label for track %s: %s → %s",
                                tk,
                                name,
                                uid_to_name[name],
                            )
                except Exception:
                    log.warning("Talk participants fetch failed", exc_info=True)

                # With no attendance evidence we cannot say who was on the
                # call, so the ordinal fallback must not put a roster name on
                # an unlabeled track — that is how a room member who never
                # joined gets quoted. Anonymous labels only.
                if not attendee_ids and others:
                    log.warning(
                        "No attendance data for room %s — suppressing the "
                        "ordinal name fallback so no roster name is guessed "
                        "onto a track",
                        room_token,
                    )
                    others = []

                labels, known_n, ord_n, speaker_n, conflicts = resolve_stream_labels(
                    track_keys,
                    labels,
                    others,
                    authoritative_keys=set(sig_labels or {}),
                )
                log.info(
                    "Label resolution: known=%d ordinal=%d speaker_n=%d conflicts=%d -> %s",
                    known_n,
                    ord_n,
                    speaker_n,
                    conflicts,
                    labels,
                )

                # Sanity gate. If we captured fewer tracks than there were
                # people ON THE CALL, the timeline covers only part of the
                # room. Feeding that to the transcriber is actively harmful:
                # the prompt calls the timeline "highly reliable" and lets it
                # override the model's own voice diarization, so a partial
                # timeline pins other people's words on whoever we did
                # capture. Better to say nothing than to say something wrong.
                #
                # Compare against observed ATTENDEES, not room membership. A
                # two-person call in a four-member room is complete with two
                # tracks; comparing against the roster would discard a
                # perfectly good timeline on most real calls.
                expected = len(attendee_ids) if attendee_ids else 0
                if expected and len(track_keys) < expected:
                    log.error(
                        "Speaker attribution INCOMPLETE: captured %d audio track(s) "
                        "for %d attendee(s) (%s). Discarding the speaker timeline "
                        "so transcription falls back to voice-based diarization "
                        "instead of trusting a partial one.",
                        len(track_keys),
                        expected,
                        sorted(attendee_ids) if attendee_ids else others,
                    )
                    return_events_suppressed = True
                else:
                    return_events_suppressed = False

                # Build speaker_events. Any trackKey not in `labels` (e.g. an
                # event from a track that vanished before extraction) gets a
                # safe Speaker N label. Seed the counter past the ones already
                # handed out so two different tracks can't both be "Speaker 1".
                fallback_counter = speaker_n
                for ev in raw_events:
                    tk = ev.get("trackKey")
                    label = labels.get(tk)
                    if not label:
                        fallback_counter += 1
                        label = f"Speaker {fallback_counter}"
                        labels[tk] = label
                    speaker_events.append(
                        {
                            "start_ms": int(ev.get("startMs", 0)),
                            "end_ms": int(ev.get("endMs", 0)),
                            "label": label,
                        }
                    )

                if return_events_suppressed:
                    speaker_events = []
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

        log.info(
            "Call attendees observed in-call (%d): %s",
            len(attendee_ids),
            sorted(attendee_ids),
        )
        return output_path, speaker_events, attendee_ids
