# MeshCore plugin installed

MeshCore includes a Hermes dashboard tab at `/meshcore`.

## Required manual dashboard trust step

Current Hermes versions block Python backend APIs from user-installed dashboard
plugins by default. This is a security boundary: importing a plugin's
`dashboard/api.py` executes Python code inside the dashboard process.

MeshCore's dashboard needs that backend API, so explicitly trust it:

```bash
python3 - <<'PY'
from pathlib import Path
import yaml
p = Path.home() / '.hermes' / 'config.yaml'
cfg = yaml.safe_load(p.read_text()) or {}
dashboard = cfg.setdefault('dashboard', {})
trusted = dashboard.get('trusted_plugin_apis')
if trusted is None:
    trusted = []
elif isinstance(trusted, str):
    trusted = [x for x in trusted.replace(',', ' ').split() if x]
elif not isinstance(trusted, list):
    trusted = []
if 'meshcore-platform' not in trusted:
    trusted.append('meshcore-platform')
dashboard['trusted_plugin_apis'] = trusted
p.write_text(yaml.safe_dump(cfg, sort_keys=False))
print('dashboard.trusted_plugin_apis =', trusted)
PY
```

Then restart the dashboard:

```bash
systemctl --user restart hermes-dashboard.service
```

Verify:

```bash
curl -s http://127.0.0.1:9119/api/dashboard/plugins \
  | jq '.[] | select(.name=="meshcore-platform") | {name, source, has_api}'
```

Expected:

```json
{"name":"meshcore-platform","source":"user","has_api":true}
```

If `has_api` is `false`, the dashboard tab will show 404 errors for
`/api/plugins/meshcore-platform/status`.
