from notetaker.recorder import _slugify, AudioRecorder


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
    assert path.endswith(".webm")
