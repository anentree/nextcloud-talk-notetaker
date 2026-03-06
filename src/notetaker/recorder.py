from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
from datetime import date

log = logging.getLogger(__name__)


def build_pulseaudio_commands(sink_name: str) -> dict[str, str]:
    """Build shell commands for PulseAudio virtual sink management."""
    return {
        "create_sink": (
            f"pactl load-module module-null-sink "
            f"sink_name={sink_name} "
            f"sink_properties=device.description={sink_name}"
        ),
        "record": (
            f"parec --device={sink_name}.monitor "
            f"--format=s16le --rate=44100 --channels=1"
        ),
        "cleanup": "pactl unload-module module-null-sink",
    }


def _slugify(name: str) -> str:
    """Convert a conversation name to a filename-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


class AudioRecorder:
    """Records audio from a Nextcloud Talk call via Playwright and PulseAudio.

    Launches a headless Chromium browser, logs into Nextcloud as the bot user,
    joins the specified Talk call, and records the browser's audio output
    through a PulseAudio virtual sink.
    """

    def __init__(
        self,
        nextcloud_url: str,
        user: str,
        password: str,
        audio_dir: str,
    ) -> None:
        self.nextcloud_url = nextcloud_url.rstrip("/")
        self.user = user
        self.password = password
        self.audio_dir = audio_dir
        os.makedirs(audio_dir, exist_ok=True)

    def _output_path(self, token: str, conversation_name: str) -> str:
        """Build the output WAV file path for a recording."""
        slug = _slugify(conversation_name) or token
        filename = f"{date.today().isoformat()}-{slug}.wav"
        return os.path.join(self.audio_dir, filename)

    async def record_call(self, room_token: str, conversation_name: str) -> str:
        """Join a Talk call, record audio, return path to WAV file.

        Blocks until the call ends (all other participants leave).
        """
        output_path = self._output_path(room_token, conversation_name)
        sink_name = f"notetaker_{room_token}"

        # Create PulseAudio virtual sink
        module_id = subprocess.check_output(
            [
                "pactl",
                "load-module",
                "module-null-sink",
                f"sink_name={sink_name}",
                f"sink_properties=device.description={sink_name}",
            ],
            text=True,
        ).strip()
        log.info("Created PulseAudio sink %s (module %s)", sink_name, module_id)

        # Start recording from the sink's monitor to a raw PCM file
        raw_fd, raw_path = tempfile.mkstemp(
            suffix=".raw", prefix=f"notetaker_{room_token}_"
        )
        os.close(raw_fd)
        with open(raw_path, "wb") as raw_file:
            parec = subprocess.Popen(
                [
                    "parec",
                    f"--device={sink_name}.monitor",
                    "--format=s16le",
                    "--rate=44100",
                    "--channels=1",
                ],
                stdout=raw_file,
            )

        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=[
                        "--use-fake-ui-for-media-stream",
                        "--use-fake-device-for-media-stream",
                        "--no-sandbox",
                    ],
                )
                context = await browser.new_context(
                    ignore_https_errors=True,
                )
                page = await context.new_page()

                # Log in to Nextcloud
                await page.goto(f"{self.nextcloud_url}/login")
                await page.fill('input[name="user"]', self.user)
                await page.fill('input[name="password"]', self.password)
                await page.click('button[type="submit"], input[type="submit"]')
                await page.wait_for_url("**/apps/**", timeout=30000)
                log.info("Logged in to Nextcloud as %s", self.user)

                # Navigate to the Talk call
                await page.goto(f"{self.nextcloud_url}/apps/spreed/{room_token}")
                await page.wait_for_load_state("networkidle")

                # Join the call
                join_btn = page.locator(
                    'button:has-text("Join call"), button:has-text("Start call")'
                )
                await join_btn.click(timeout=15000)
                log.info("Joined call in room %s", room_token)

                # Move browser audio output to our virtual sink
                await asyncio.sleep(2)
                try:
                    subprocess.run(
                        "pacmd list-sink-inputs "
                        "| grep -B 20 'application.name.*Chrom' "
                        "| grep 'index:' | tail -1 "
                        f"| awk '{{print $2}}' "
                        f"| xargs -I{{}} pacmd move-sink-input {{}} {sink_name}",
                        shell=True,
                        capture_output=True,
                        text=True,
                    )
                    log.info("Moved browser audio to sink %s", sink_name)
                except Exception:
                    log.warning("Could not move sink input, audio may not record")

                # Wait for call to end
                while True:
                    await asyncio.sleep(5)
                    join_visible = await page.locator(
                        'button:has-text("Join call"), button:has-text("Start call")'
                    ).is_visible()
                    if join_visible:
                        log.info("Call ended in room %s", room_token)
                        break
                    disconnected = await page.locator(
                        'text="You have been disconnected"'
                    ).is_visible()
                    if disconnected:
                        log.info("Disconnected from call in room %s", room_token)
                        break

                await browser.close()
        finally:
            # Stop recording
            parec.terminate()
            parec.wait()

            # Convert raw PCM to WAV
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "s16le",
                    "-ar",
                    "44100",
                    "-ac",
                    "1",
                    "-i",
                    raw_path,
                    output_path,
                ],
                capture_output=True,
            )

            # Clean up raw file and PulseAudio module
            if os.path.exists(raw_path):
                os.remove(raw_path)
            subprocess.run(
                ["pactl", "unload-module", module_id],
                capture_output=True,
            )
            log.info("Cleaned up PulseAudio sink %s", sink_name)

        log.info("Audio saved to %s", output_path)
        return output_path
