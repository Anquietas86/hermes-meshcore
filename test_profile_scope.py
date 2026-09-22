import adapter


def test_requirements_use_the_active_profile_scope(monkeypatch):
    monkeypatch.delenv("MESHCORE_HOST", raising=False)
    monkeypatch.setattr(
        adapter,
        "_get_scoped_secret",
        lambda name, default="": (
            "openhop.lan.hagger.id.au" if name == "MESHCORE_HOST" else default
        ),
    )

    assert adapter.check_requirements() is True


def test_env_enablement_reads_scoped_profile_values(monkeypatch):
    monkeypatch.delenv("MESHCORE_HOST", raising=False)
    monkeypatch.setattr(
        adapter,
        "_get_scoped_secret",
        lambda name, default="": {
            "MESHCORE_HOST": "openhop.lan.hagger.id.au",
            "MESHCORE_PORT": "5001",
            "MESHCORE_ENABLE_DMS": "true",
        }.get(name, default),
    )

    enabled = adapter._env_enablement()

    assert enabled is not None
    assert enabled["host"] == "openhop.lan.hagger.id.au"
    assert enabled["port"] == 5001
    assert enabled["enable_dms"] == "true"
