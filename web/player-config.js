// Serve the player, stream and API through one HTTPS origin (see config/Caddyfile.example).
// For embedding elsewhere, replace these with the deployed HTTPS addresses.
window.WIT_RADIO_CONFIG = {
  streamUrl: "/stream",
  apiBase: "/api"
};
