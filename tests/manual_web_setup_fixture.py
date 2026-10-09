"""Disposable, fake-only manual UI check. Never prepares a real installation.

Run only when a local browser test is authorized. This is not a substitute route
around a browser block. Stop on any browser security denial or warning.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import multidot_setup as setup
import multidot_web as web
import multidot_wizard as wizard

MARKER = "WEB_SETUP_FAKE_KEY_ONLY"
BANNER = ("<p><strong>FAKE-ONLY CHECK.</strong> Enter " + MARKER +
          ". Never enter a real key. No real configuration or credentials "
          "will be created.</p>")
GUARD = r'''
<script nonce="__NONCE__">
(() => {
  const form = document.getElementById("setup");
  const dots = document.getElementById("dots");
  const marker = "WEB_SETUP_FAKE_KEY_ONLY";
  function fillFixtures() {
    Array.from(dots.children).forEach((row, index) => {
      const name = row.querySelector('[data-field="name"]');
      const tunnel = row.querySelector('[data-field="tunnel_id"]');
      const key = row.querySelector('[data-field="runtime_api_key"]');
      name.value = "Fixture dot " + (index + 1);
      tunnel.value = "tunnel_" + (index + 1).toString(16).padStart(32, "0");
      name.readOnly = true;
      tunnel.readOnly = true;
      key.placeholder = marker;
    });
  }
  new MutationObserver(fillFixtures).observe(dots, {childList: true});
  fillFixtures();
  form.addEventListener("submit", event => {
    const fields = [...form.querySelectorAll('[data-field="runtime_api_key"]')];
    if (fields.some(input => input.value !== marker)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      fields.forEach(input => { input.value = ""; });
      document.getElementById("status").textContent =
        "Only the displayed fake marker is accepted. Never enter a real key.";
    }
  }, true);
})();
</script>
'''


def validate_fixture(data):
    """Reject every nonfixture value before the real atomic publisher sees it."""
    try:
        spec = json.loads(data)
        rows = setup.validate_dots(spec)
        for index, row in enumerate(rows, 1):
            if (row["name"] != "Fixture dot " + str(index) or
                    row["tunnel_id"] != "tunnel_" + format(index, "032x") or
                    row["runtime_api_key"] != MARKER or row["role"] != "worker"):
                raise ValueError
    except Exception:
        raise setup.SetupError("fixture_values_only") from None


class FixtureServer(web._Server):
    def page(self):
        page = super().page().decode("utf-8")
        page = page.replace("<h1>MultiDot local setup</h1>",
                            "<h1>MultiDot local setup</h1>" + BANNER)
        page = page.replace(
            "Configuration saved. Local preparation continues in your terminal. You can close this page.",
            "Fake-only save passed. No runtime setup was performed. You can close this page.")
        return page.replace("</body>", GUARD.replace("__NONCE__", self.nonce) + "</body>").encode("utf-8")


def main():
    os.umask(0o077)
    original_destination = wizard.private_destination

    @contextmanager
    def fixture_destination(path):
        with original_destination(path) as publish:
            def publish_fixture(data, result):
                validate_fixture(data)
                publish(data, result)
            yield publish_fixture

    with tempfile.TemporaryDirectory(prefix="multidot-manual-web-fixture-") as directory:
        home = Path(directory) / "home"
        home.mkdir(mode=0o700)
        with patch.dict(os.environ, {"HOME": str(home)}), \
                patch.object(wizard, "private_destination", fixture_destination), \
                patch.object(web, "_Server", FixtureServer):
            print("FAKE-ONLY browser check. Use " + MARKER + "; never a real key.", flush=True)
            try:
                result = web.collect_and_save(setup.default_config(), state_root=home / "unused-state")
                passed = bool(result["saved"] and result["requested_setup"])
                print(json.dumps({"fake_only": True, "save_passed": passed,
                                  "configured_dots": result["configured_dots"],
                                  "preparation_skipped": True, "fixture_removed_on_exit": True}))
                return 0
            except setup.SetupError as error:
                print(json.dumps({"fake_only": True, "error": error.code,
                                  "preparation_skipped": True}), file=sys.stderr)
                return 2


if __name__ == "__main__":
    raise SystemExit(main())
