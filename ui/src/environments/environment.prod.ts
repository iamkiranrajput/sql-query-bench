// Production environment. apiUrl is computed at runtime from the page's
// hostname, so the same build artifact works on localhost and any internal
// hostname without rebuilding. UI is served on port 4280 and calls FastAPI
// on port 8090 of the SAME host — that origin must be listed in
// server/.env ALLOWED_ORIGINS.
export const environment = {
  production: true,
  apiUrl: `http://${window.location.hostname}:8090`,
  apiKey: ''   // Set to match API_KEY in server/.env (empty = auth disabled)
};
