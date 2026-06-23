from electridrive import config, licensing


def test_default_scope_is_drive_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("ELECTRIDRIVE_SCOPE", raising=False)
    assert config.selected_scopes() == [config.DEFAULT_SCOPE]


def test_env_drive_override_bypasses_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("ELECTRIDRIVE_SCOPE", "drive")
    assert config.selected_scopes() == [config.FULL_DRIVE_SCOPE]


def test_settings_drive_without_license_is_gated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("ELECTRIDRIVE_SCOPE", raising=False)
    config.save_settings(config.Settings(scope="drive"))
    # no license, no own credentials.json -> falls back to safe per-file
    assert config.selected_scopes() == [config.DEFAULT_SCOPE]


def test_settings_drive_with_credentials_is_full(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("ELECTRIDRIVE_SCOPE", raising=False)
    config.save_settings(config.Settings(scope="drive"))
    (config.get_paths().credentials_file).write_text("{}", encoding="utf-8")
    assert config.selected_scopes() == [config.FULL_DRIVE_SCOPE]


def test_settings_drive_with_valid_license_is_full(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("ELECTRIDRIVE_SCOPE", raising=False)
    priv, pub = licensing.generate_keypair()
    monkeypatch.setattr(licensing, "PUBLIC_KEY_B64", pub)
    key = licensing.sign("Tester", "t@example.com", priv)
    config.save_settings(config.Settings(scope="drive", license_key=key))
    assert config.selected_scopes() == [config.FULL_DRIVE_SCOPE]


def test_env_file_overrides_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.save_settings(config.Settings(scope="drive"))
    monkeypatch.setenv("ELECTRIDRIVE_SCOPE", "file")
    assert config.selected_scopes() == [config.DEFAULT_SCOPE]


def test_settings_roundtrip_includes_scope_and_license(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.save_settings(config.Settings(scope="drive", theme="electric_light",
                                        license_key="abc.def"))
    s = config.load_settings()
    assert s.scope == "drive" and s.theme == "electric_light" and s.license_key == "abc.def"
