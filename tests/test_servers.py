from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from shiplord.cli import app
from shiplord.servers import (ServerError, SSHCheckError, list_servers, load_server, register_server, remove_server,
                              rename_server)

MANIFEST = '''schema_version = 1
name = "orders"
server = "production"
template = "generic"
runtime = "docker"
[http]
api_domain = "orders-api.example.com"
frontend_origins = []
container_port = 8080
readiness_path = "/ready"
[resources]
memory_mb = 512
[deploy]
readiness_timeout_seconds = 90
retain_successful_releases = 2
'''


class ServerRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        ssh = self.bin / "ssh"
        ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$SSH_TEST_ARGS"\nexit "${SSH_TEST_EXIT:-0}"\n')
        ssh.chmod(0o700)
        self.env = {"XDG_CONFIG_HOME": str(self.root / "config"), "PATH": str(self.bin),
                    "SSH_TEST_ARGS": str(self.root / "arguments")}

    def invoke(self, *args):
        with patch.dict(os.environ, self.env):
            return CliRunner().invoke(app, ["server", "add", *args])

    def test_registration_uses_strict_fresh_ssh_and_private_storage(self):
        result = self.invoke("production", "--ssh", "appuser@ubuntu-4gb-hel1-1")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Run `shiplord server bootstrap` next", result.output)
        options = (self.root / "arguments").read_text().splitlines()
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "VerifyHostKeyDNS=no",
                       "UpdateHostKeys=no", "ControlMaster=no", "ControlPath=none",
                       "ClearAllForwardings=yes", "ForwardAgent=no", "RequestTTY=no", "ConnectTimeout=10"):
            self.assertIn(option, options)
        self.assertEqual(options[-3:], ["--", "appuser@ubuntu-4gb-hel1-1", "true"])
        servers = self.root / "config/shiplord/servers"
        saved = servers / "production.json"
        self.assertEqual(json.loads(saved.read_text()), {"schema_version": 1, "alias": "production", "ssh": "appuser@ubuntu-4gb-hel1-1"})
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(servers.stat().st_mode & 0o777, 0o700)
        self.assertEqual([item.name for item in servers.iterdir()], ["production.json"])

    def test_invalid_target_never_invokes_ssh(self):
        targets = ["-option", "user@-option", "user@host;touch", "user@host name", "user@a@b", "user@host\n", "--user@example.com", "user@fe80::1%en0"]
        for target in targets:
            with self.subTest(target=target):
                result = self.invoke("production", "--ssh", target)
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertFalse((self.root / "arguments").exists())

    def test_ipv4_and_ipv6_targets_are_passed_as_single_arguments(self):
        for alias, target in (("ipv4", "appuser@127.0.0.1"), ("ipv6", "appuser@::1")):
            with self.subTest(target=target):
                result = self.invoke(alias, "--ssh", target)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual((self.root / "arguments").read_text().splitlines()[-2], target)

    def test_failed_ssh_does_not_register_alias(self):
        with patch.dict(os.environ, dict(self.env, SSH_TEST_EXIT="255")):
            result = CliRunner().invoke(app, ["server", "add", "production", "--ssh", "appuser@example.com"])
        self.assertEqual(result.exit_code, 3)
        self.assertFalse((self.root / "config/shiplord/servers/production.json").exists())

    def test_duplicate_alias_preserves_original(self):
        first = self.invoke("production", "--ssh", "appuser@example.com")
        second = self.invoke("production", "--ssh", "other@example.com")
        self.assertEqual(first.exit_code, 0, first.output)
        self.assertEqual(second.exit_code, 1, second.output)
        saved = self.root / "config/shiplord/servers/production.json"
        self.assertEqual(json.loads(saved.read_text())["ssh"], "appuser@example.com")
        self.assertEqual([item.name for item in saved.parent.iterdir()], ["production.json"])

    def test_ssh_timeout_and_missing_executable_are_reported(self):
        for error in (subprocess.TimeoutExpired("ssh", 15), FileNotFoundError()):
            with self.subTest(error=type(error).__name__), patch("subprocess.run", side_effect=error):
                with self.assertRaises(SSHCheckError):
                    register_server("production", "appuser@example.com")
        self.assertFalse((self.root / "config").exists())

    def test_untrusted_config_paths_do_not_receive_aliases(self):
        config = self.root / "config"
        config.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (config / "shiplord").symlink_to(outside, target_is_directory=True)
        result = self.invoke("production", "--ssh", "appuser@example.com")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(list(outside.iterdir()), [])

    def test_insecure_directory_permissions_are_rejected(self):
        directory = self.root / "config/shiplord"
        directory.mkdir(parents=True, mode=0o755)
        directory.chmod(0o755)
        result = self.invoke("production", "--ssh", "appuser@example.com")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertFalse((directory / "servers/production.json").exists())

    def test_relative_config_path_is_rejected(self):
        with patch.dict(os.environ, dict(self.env, XDG_CONFIG_HOME="relative")):
            result = CliRunner().invoke(app, ["server", "add", "production", "--ssh", "appuser@example.com"])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("absolute path", result.output)

    def test_failed_write_does_not_publish_an_alias(self):
        with patch.dict(os.environ, self.env), patch("shiplord.servers.os.fsync", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(ServerError, "could not save"):
                register_server("production", "appuser@example.com")
        directory = self.root / "config/shiplord/servers"
        self.assertEqual(list(directory.iterdir()), [])


class ServerListTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.servers = self.config / "shiplord/servers"
        self.env = {"XDG_CONFIG_HOME": str(self.config)}

    def save(self, alias, target, *, name=None, mode=0o600):
        self.servers.mkdir(parents=True, exist_ok=True)
        self.config.chmod(0o700)
        (self.config / "shiplord").chmod(0o700)
        self.servers.chmod(0o700)
        path = self.servers / (name or alias + ".json")
        path.write_text(json.dumps({"schema_version": 1, "alias": alias, "ssh": target}))
        path.chmod(mode)
        return path

    def invoke(self, *args):
        with patch.dict(os.environ, self.env):
            return CliRunner().invoke(app, ["server", "list", *args])

    def test_nothing_registered_lists_nothing_and_creates_no_directory(self):
        with patch.dict(os.environ, self.env):
            self.assertEqual(list_servers(), ([], 0))
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("shiplord server add", result.output)
        self.assertFalse(self.config.exists())

    def test_registered_servers_are_listed_sorted_by_alias(self):
        self.save("staging", "deploy@staging.example.com")
        self.save("production", "admin@203.0.113.7")
        with patch.dict(os.environ, self.env):
            self.assertEqual(list_servers(), ([{"alias": "production", "ssh": "admin@203.0.113.7"},
                                               {"alias": "staging", "ssh": "deploy@staging.example.com"}], 0))
        lines = self.invoke().output.splitlines()
        self.assertEqual(lines, ["production  admin@203.0.113.7", "staging     deploy@staging.example.com"])

    def test_json_output_has_servers_and_invalid_count(self):
        self.save("production", "admin@example.com")
        result = self.invoke("--json")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output), {"ok": True, "invalid_entries": 0,
                                                     "servers": [{"alias": "production", "ssh": "admin@example.com"}]})

    def test_unreadable_or_untrusted_entries_are_counted_but_never_shown(self):
        self.save("production", "admin@example.com")
        self.save("readable", "admin@example.com", mode=0o644)               # not private
        self.save("mismatch", "admin@example.com", name="other.json")        # alias does not match file name
        self.save("\x1b[31mevil", "admin@example.com", name="\x1b[31mevil.json")  # self-consistent, invalid alias
        (self.servers / "corrupt.json").write_text("{not json")
        (self.servers / "corrupt.json").chmod(0o600)
        (self.servers / "huge.json").write_text("x" * 5000)
        (self.servers / "huge.json").chmod(0o600)
        (self.servers / "linked.json").symlink_to(self.servers / "production.json")
        os.mkfifo(self.servers / "fifo.json", 0o600)
        (self.servers / "badtarget.json").write_text(json.dumps({"schema_version": 1, "alias": "badtarget", "ssh": "-oProxy"}))
        (self.servers / "badtarget.json").chmod(0o600)
        with patch.dict(os.environ, self.env):
            servers, invalid = list_servers()
        self.assertEqual(servers, [{"alias": "production", "ssh": "admin@example.com"}])
        self.assertEqual(invalid, 8)
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("\x1b", result.output)
        self.assertNotIn("readable", result.output)
        self.assertIn("8 invalid entries ignored", result.output)

    def test_interrupted_registration_temporary_files_are_ignored(self):
        self.save("production", "admin@example.com")
        self.save("production", "admin@example.com", name=".new-" + "0" * 32)
        with patch.dict(os.environ, self.env):
            self.assertEqual(list_servers(), ([{"alias": "production", "ssh": "admin@example.com"}], 0))

    def test_insecure_directory_is_an_error_not_an_empty_list(self):
        self.save("production", "admin@example.com")
        self.servers.chmod(0o755)
        with patch.dict(os.environ, self.env):
            with self.assertRaises(ServerError):
                list_servers()
        result = self.invoke("--json")
        self.assertEqual(result.exit_code, 3, result.output)
        self.assertNotIn(str(self.root), result.output)

    def test_load_server_still_fails_cleanly_when_nothing_is_registered(self):
        with patch.dict(os.environ, self.env):
            with self.assertRaisesRegex(ServerError, "could not access"):
                load_server("production")
        self.assertFalse(self.config.exists())


class ServerAliasChangeTests(unittest.TestCase):
    """Removal and rename run in an application directory, where ship.toml can name an alias."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.servers = self.config / "shiplord/servers"
        self.app = self.root / "app"
        self.app.mkdir()
        self.env = {"XDG_CONFIG_HOME": str(self.config)}
        previous = os.getcwd()
        os.chdir(self.app)
        self.addCleanup(os.chdir, previous)

    def save(self, alias, target, *, name=None, contents=None):
        self.servers.mkdir(parents=True, exist_ok=True)
        for directory in (self.config, self.config / "shiplord", self.servers):
            directory.chmod(0o700)
        path = self.servers / (name or alias + ".json")
        path.write_text(contents or json.dumps({"schema_version": 1, "alias": alias, "ssh": target}))
        path.chmod(0o600)
        return path

    def invoke(self, command, *args):
        with patch.dict(os.environ, self.env):
            return CliRunner().invoke(app, ["server", command, *args])

    def manifest(self, server="production"):
        (self.app / "ship.toml").write_text(MANIFEST.replace('server = "production"', f'server = "{server}"'))

    def names(self):
        return sorted(item.name for item in self.servers.iterdir())

    def test_remove_deletes_the_alias(self):
        self.save("production", "admin@example.com")
        result = self.invoke("remove", "production")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Removed server alias 'production'.", result.output)
        self.assertEqual(self.names(), [])

    def test_remove_json_reports_the_alias(self):
        self.save("production", "admin@example.com")
        result = self.invoke("remove", "production", "--json")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output), {"ok": True, "removed": "production"})
        self.assertEqual(self.names(), [])

    def test_remove_refuses_while_the_current_manifest_uses_the_alias(self):
        self.save("production", "admin@example.com")
        self.manifest()
        result = self.invoke("remove", "production", "--json")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("in use by ship.toml", json.loads(result.output)["error"])
        self.assertEqual(self.names(), ["production.json"])

    def test_remove_proceeds_when_the_manifest_names_another_alias(self):
        self.save("production", "admin@example.com")
        self.manifest("staging")
        result = self.invoke("remove", "production")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.names(), [])

    def test_remove_refuses_an_unreadable_manifest_before_touching_the_alias(self):
        self.save("production", "admin@example.com")
        (self.app / "ship.toml").write_text("not = [valid")
        result = self.invoke("remove", "production", "--json")
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("could not check whether the server alias is in use", json.loads(result.output)["error"])
        self.assertEqual(self.names(), ["production.json"])

    def test_remove_rejects_an_invalid_alias_before_any_side_effect(self):
        self.save("production", "admin@example.com")
        for alias in ("Production", "bad--alias", "with space", "\x1b[31mevil"):
            with self.subTest(alias=alias):
                result = self.invoke("remove", alias)
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertEqual(self.names(), ["production.json"])

    def test_remove_unknown_alias_keeps_the_registration(self):
        self.save("production", "admin@example.com")
        result = self.invoke("remove", "staging", "--json")
        self.assertEqual(result.exit_code, 3, result.output)
        self.assertIn("not registered", json.loads(result.output)["error"])
        self.assertEqual(self.names(), ["production.json"])

    def test_remove_without_registered_servers_creates_nothing(self):
        result = self.invoke("remove", "production")
        self.assertEqual(result.exit_code, 3, result.output)
        self.assertFalse(self.config.exists())

    def test_remove_server_reports_an_in_use_alias_without_deleting_it(self):
        self.save("production", "admin@example.com")
        self.manifest()
        with patch.dict(os.environ, self.env):
            with self.assertRaisesRegex(ServerError, "in use by ship.toml"):
                remove_server("production")
        self.assertEqual(self.names(), ["production.json"])

    def test_rename_moves_the_registration_and_keeps_its_target(self):
        self.save("production", "admin@example.com")
        result = self.invoke("rename", "production", "prod")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Renamed server alias 'production' to 'prod'.", result.output)
        self.assertEqual(self.names(), ["prod.json"])
        self.assertEqual(json.loads((self.servers / "prod.json").read_text()),
                         {"schema_version": 1, "alias": "prod", "ssh": "admin@example.com"})

    def test_rename_json_reports_both_aliases(self):
        self.save("production", "admin@example.com")
        result = self.invoke("rename", "production", "prod", "--json")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output), {"ok": True, "alias": "prod", "previous_alias": "production"})
        self.assertEqual(self.names(), ["prod.json"])

    def test_rename_refuses_an_existing_new_alias_without_changing_either(self):
        self.save("production", "admin@example.com")
        self.save("staging", "deploy@staging.example.com")
        result = self.invoke("rename", "production", "staging", "--json")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("already registered", json.loads(result.output)["error"])
        self.assertEqual(self.names(), ["production.json", "staging.json"])
        self.assertEqual(json.loads((self.servers / "production.json").read_text())["ssh"], "admin@example.com")
        self.assertEqual(json.loads((self.servers / "staging.json").read_text())["ssh"], "deploy@staging.example.com")

    def test_rename_refuses_an_unreadable_existing_entry_rather_than_replacing_it(self):
        self.save("production", "admin@example.com")
        self.save("staging", "ignored", contents="{not json")
        result = self.invoke("rename", "production", "staging")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual((self.servers / "staging.json").read_text(), "{not json")
        self.assertTrue((self.servers / "production.json").exists())

    def test_rename_refuses_the_same_alias(self):
        self.save("production", "admin@example.com")
        result = self.invoke("rename", "production", "production", "--json")
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("same as the old alias", json.loads(result.output)["error"])
        self.assertEqual(self.names(), ["production.json"])

    def test_rename_rejects_invalid_aliases_before_any_side_effect(self):
        self.save("production", "admin@example.com")
        for old, new in (("production", "Bad"), ("Bad", "prod"), ("production", "bad--alias")):
            with self.subTest(old=old, new=new):
                result = self.invoke("rename", old, new)
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertEqual(self.names(), ["production.json"])

    def test_rename_unknown_old_alias_creates_nothing(self):
        self.save("production", "admin@example.com")
        result = self.invoke("rename", "missing", "prod")
        self.assertEqual(result.exit_code, 3, result.output)
        self.assertEqual(self.names(), ["production.json"])

    def test_rename_without_registered_servers_creates_nothing(self):
        result = self.invoke("rename", "production", "prod")
        self.assertEqual(result.exit_code, 3, result.output)
        self.assertFalse(self.config.exists())

    def test_rename_rolls_back_the_new_alias_when_removing_the_old_one_fails(self):
        self.save("production", "admin@example.com")
        real_unlink = os.unlink

        def failing_unlink(path, *, dir_fd=None):
            if path == "production.json":
                raise OSError("injected failure")
            return real_unlink(path, dir_fd=dir_fd)

        with patch.dict(os.environ, self.env), patch("shiplord.servers.os.unlink", side_effect=failing_unlink):
            with self.assertRaisesRegex(ServerError, "old alias was kept"):
                rename_server("production", "prod")
        self.assertEqual(self.names(), ["production.json"])
        self.assertEqual(json.loads((self.servers / "production.json").read_text())["alias"], "production")
