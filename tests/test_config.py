import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from compat_review import config, service

CONVENTIONAL = {"GITHUB_TOKEN", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_PRIVATE_KEY_FILE",
                "GITHUB_WEBHOOK_SECRET", "OPENAI_BASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PORT"}
CLEAN = {k: v for k, v in os.environ.items() if not k.startswith("COMPAT_REVIEW_") and k not in CONVENTIONAL}


@contextlib.contextmanager
def isolated(**env):
    """A clean environment plus `env`, in an empty working directory."""
    with mock.patch.dict(os.environ, {**CLEAN, **env}, clear=True), tempfile.TemporaryDirectory() as tmp, \
            contextlib.chdir(tmp):
        yield Path(tmp)


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        with isolated():
            cfg = config.load()
            self.assertEqual((cfg.MODEL, cfg.API, cfg.EFFORT, cfg.DATA, cfg.PORT), ("", "responses", "medium", "data", 8000))
            self.assertEqual((cfg.POLL_INTERVAL, cfg.POLL_MAX_AGE_DAYS, cfg.MAX_MIRRORS_GB, cfg.FEEDBACK_INTERVAL), (60, 7, 50, 1800))
            self.assertTrue(cfg.SKIP_DRAFTS)
            self.assertIsNone(cfg.GITHUB_TOKEN)
            self.assertIsNone(cfg.BASE_URL)
            self.assertEqual((config.repos(cfg), config.headers(cfg), config.price(cfg, "PRICE_IN_PER_M")), ([], {}, None))
            self.assertIsNone(config.private_key(cfg))

    def test_environment_overrides_and_conventional_fallbacks(self):
        with isolated(COMPAT_REVIEW_MODEL="m", COMPAT_REVIEW_POLL_REPOS="a/b, c/d", COMPAT_REVIEW_HEADERS='{"x-k": "v"}',
                      COMPAT_REVIEW_SKIP_DRAFTS="0", COMPAT_REVIEW_PRICE_IN_PER_M="0.1", COMPAT_REVIEW_POLL_INTERVAL="120",
                      GITHUB_TOKEN="tok", OPENAI_BASE_URL="http://gw", OPENAI_API_KEY="k", PORT="9000"):
            cfg = config.load()
            self.assertEqual((cfg.MODEL, cfg.POLL_INTERVAL, cfg.PORT), ("m", 120, 9000))
            self.assertFalse(cfg.SKIP_DRAFTS)
            self.assertEqual(config.repos(cfg), ["a/b", "c/d"])
            self.assertEqual(config.headers(cfg), {"x-k": "v"})
            self.assertEqual(config.price(cfg, "PRICE_IN_PER_M"), 0.1)
            self.assertEqual((cfg.GITHUB_TOKEN, cfg.BASE_URL, cfg.API_KEY), ("tok", "http://gw", "k"))

    def test_prefixed_name_wins_over_conventional(self):
        with isolated(COMPAT_REVIEW_GITHUB_TOKEN="prefixed", GITHUB_TOKEN="plain"):
            self.assertEqual(config.load().GITHUB_TOKEN, "prefixed")

    def test_settings_file_found_from_a_subdirectory_and_secrets_file(self):
        with isolated() as tmp:
            (tmp / "settings.toml").write_text('model = "from-file"\npoll_repos = ["o/r"]\nheaders = { "x-k" = "v" }\n'
                                               'price_out_per_m = 0.5\ngithub_app_id = 42\n')
            (tmp / ".secrets.toml").write_text('github_token = "secret-tok"\n')
            (tmp / "sub").mkdir()
            with contextlib.chdir(tmp / "sub"):
                cfg = config.load()
            self.assertEqual((cfg.MODEL, config.repos(cfg), config.headers(cfg)), ("from-file", ["o/r"], {"x-k": "v"}))
            self.assertEqual((config.price(cfg, "PRICE_OUT_PER_M"), cfg.GITHUB_TOKEN), (0.5, "secret-tok"))
            settings = service.Settings.from_config(cfg, post=False, data_dir="elsewhere")
            self.assertEqual((settings.model, settings.app_id, settings.token, settings.price_out, str(settings.data_dir)),
                             ("from-file", "42", "secret-tok", 0.5, "elsewhere"))
            self.assertEqual(settings.max_mirror_bytes, 50 * 1024**3)

    def test_explicit_file_and_private_key_file(self):
        with isolated() as tmp:
            pem = tmp / "app.pem"
            pem.write_text("-----BEGIN KEY-----\n")
            custom = tmp / "custom.toml"
            custom.write_text(f'model = "custom"\ngithub_app_private_key_file = "{pem}"\n')
            cfg = config.load(custom)
            self.assertEqual((cfg.MODEL, config.private_key(cfg)), ("custom", "-----BEGIN KEY-----\n"))
            self.assertIs(config.current(), cfg)
            with self.assertRaises(FileNotFoundError):
                config.load(tmp / "missing.toml")

    def test_validation(self):
        with isolated(COMPAT_REVIEW_API="bogus"):
            with self.assertRaises(ValueError) as ctx:
                config.load()
            self.assertIn("API", str(ctx.exception))
        with isolated(COMPAT_REVIEW_POLL_INTERVAL="0"):
            with self.assertRaises(ValueError):
                config.load()

    def test_service_main_rejects_bad_configuration(self):
        with isolated(COMPAT_REVIEW_API="bogus"):
            with self.assertRaises(SystemExit) as ctx:
                service.main(["--once", "o/r#1"])
            self.assertIn("configuration", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
