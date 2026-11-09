import os


def get_scoped_secret(name, default=None, *, external_fallback=False):
    value = os.environ.get(name)
    return value if value is not None else default


def extra_or_secret(extra, key, env, default="", *, blank_is_unset=True):
    if env:
        value = get_scoped_secret(env, None)
        if value is not None and str(value).strip():
            return value
    value = (extra or {}).get(key)
    if value is None or (blank_is_unset and isinstance(value, str) and not value.strip()):
        return default
    return value


def seed_extra_from_env(spec, *, home_env=None, home_default=""):
    seed = {}
    for env, key, conv in spec:
        raw = str(get_scoped_secret(env, "") or "").strip()
        if not raw:
            continue
        try:
            seed[key] = conv(raw) if conv else raw
        except ValueError:
            pass
    return seed
