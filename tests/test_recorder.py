from notetaker.recorder import build_pulseaudio_commands, _slugify, AudioRecorder


def test_build_pulseaudio_commands():
    sink_name = "notetaker_abc123"
    cmds = build_pulseaudio_commands(sink_name)

    assert "pactl load-module module-null-sink" in cmds["create_sink"]
    assert sink_name in cmds["create_sink"]
    assert "parec" in cmds["record"]
    assert sink_name in cmds["record"]
    assert "pactl unload-module" in cmds["cleanup"]


def test_slugify():
    assert _slugify("Daily Standup") == "daily-standup"
    assert _slugify("  Sprint Planning #3  ") == "sprint-planning-3"
    assert _slugify("One") == "one"
    assert _slugify("") == ""


def test_output_path_format():
    recorder = AudioRecorder.__new__(AudioRecorder)
    recorder.audio_dir = "/tmp/notetaker-audio"

    path = recorder._output_path("abc123", "Daily Standup")

    assert path.startswith("/tmp/notetaker-audio/")
    assert "daily-standup" in path
    assert path.endswith(".wav")
