"""Materialize two ordinary, reproducible Git commits under a new directory."""

import argparse
import json
from pathlib import Path
import subprocess

FIXTURE = Path(__file__).with_name("engineering")


def provision(path, *, test=None, manifest=None):
    path.mkdir(mode=0o700)
    environment = {
        "PATH": "/usr/bin:/bin", "HOME": str(path), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "Nexus Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Nexus Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    }

    def git(*args):
        return subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *args],
                              cwd=path, env=environment, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()

    git("init", "--initial-branch=fixture", "--object-format=sha1", "--template=")
    for name in ("NEXUS_FIXTURE.json", "model.py", "test_model.py"):
        (path / name).write_bytes((FIXTURE / name).read_bytes())
    if test is not None:
        (path / "test_model.py").write_text(test, encoding="utf-8")
    if manifest is not None:
        (path / "NEXUS_FIXTURE.json").write_text(manifest, encoding="utf-8")
    git("add", "NEXUS_FIXTURE.json", "model.py", "test_model.py")
    git("commit", "-m", "Reproduce unit conversion defect")
    defect = git("rev-parse", "HEAD")
    model = path / "model.py"
    model.write_text(model.read_text().replace("distance * 100", "distance * 1000"), encoding="utf-8")
    git("add", "model.py")
    git("commit", "-m", "Correct unit conversion")
    control = git("rev-parse", "HEAD")
    git("checkout", "--detach", defect)
    return {"defect": defect, "control": control}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    print(json.dumps(provision(parser.parse_args().checkout.resolve()), sort_keys=True))
