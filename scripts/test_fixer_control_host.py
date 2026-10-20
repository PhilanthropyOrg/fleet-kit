"""the-fixer's check.sh must not fire PROD DOWN when the host's own network is failing.

2026-10-20: the host NAT reset about half of all connects (GitHub and Google too) and
check.sh fired "PROD DOWN (health=000)" 7 times in 7 hours, each pass ending "no PR opened".
These run the control-host block from check.sh itself under bash with a fake curl.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

CHECK = Path(__file__).resolve().parent.parent / "members/the-fixer/check.sh"


def _control_block():
    lines = CHECK.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip().startswith('CONTROL_URL="'))
    done = next(i for i in range(start, len(lines)) if lines[i].strip() == "done")
    return "\n".join(lines[start:done + 2])  # through the closing `fi` of the if


def _run(codes, prod_down="health=000", control_env=None):
    """codes: what the fake curl prints on each call, in order."""
    with tempfile.TemporaryDirectory() as d:
        seq = Path(d) / "codes"
        seq.write_text("\n".join(codes) + "\n")
        curl = Path(d) / "curl"
        curl.write_text(
            "#!/bin/bash\n"
            f'c=$(head -1 "{seq}"); sed -i 1d "{seq}"; printf "%s" "$c"\n'
        )
        curl.chmod(0o755)
        script = (
            'log() { :; }\nPROD_NOTE=""\n'
            f'PROD_DOWN="{prod_down}"\n{_control_block()}\n'
            'echo "DOWN=[$PROD_DOWN] NOTE=[$PROD_NOTE]"\n'
        )
        env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}")
        env.pop("FIXER_CONTROL_URL", None)
        if control_env is not None:
            env["FIXER_CONTROL_URL"] = control_env
        out = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        return out.stdout.strip(), len(seq.read_text().split())


class ControlHost(unittest.TestCase):
    def test_control_host_down_means_no_fire(self):
        out, _ = _run(["000"])
        self.assertIn("DOWN=[]", out)
        self.assertIn("control host failed 000", out)

    def test_flaky_network_second_probe_fails_means_no_fire(self):
        out, _ = _run(["204", "000"])
        self.assertIn("DOWN=[]", out)

    def test_healthy_network_keeps_the_real_fire(self):
        out, left = _run(["204", "204"])
        self.assertIn("DOWN=[health=000]", out)
        self.assertIn("NOTE=[]", out)
        self.assertEqual(left, 0, "both control probes ran")

    def test_no_prod_down_never_probes_the_control_host(self):
        out, left = _run(["000"], prod_down="")
        self.assertIn("DOWN=[] NOTE=[]", out)
        self.assertEqual(left, 1)

    def test_empty_control_url_turns_the_guard_off(self):
        out, left = _run(["000"], control_env="")
        self.assertIn("DOWN=[health=000]", out)
        self.assertEqual(left, 1)

    def test_block_sits_before_the_fire_decision(self):
        text = CHECK.read_text()
        self.assertLess(text.index('CONTROL_URL="'), text.index('FIRE_SHA="prod-'))


if __name__ == "__main__":
    unittest.main()
