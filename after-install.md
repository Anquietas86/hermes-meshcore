# MeshCore plugin installed

1. Point it at your node and pick who may talk to it, in `~/.hermes/.env`:

   ```bash
   MESHCORE_HOST=192.168.1.50
   MESHCORE_ALLOWED_USERS=<your pubkey, or its first 12+ hex chars>
   MESHCORE_ADMIN_NODES=<your pubkey>
   ```

2. Enable the plugin. This also mounts the dashboard tab's backend:

   ```bash
   hermes plugins enable meshcore-platform
   ```

3. Restart the gateway and dashboard:

   ```bash
   hermes gateway restart
   ```

The **MeshCore** tab appears in the dashboard. If it shows "No such API
endpoint", the plugin is not in `plugins.enabled` for that profile.
