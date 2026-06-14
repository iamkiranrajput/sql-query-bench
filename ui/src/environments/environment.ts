export const environment = {
  production: false,
  apiUrl: `http://${window.location.hostname}:8090`,
  apiKey: ''  // Set to match API_KEY in server/.env (empty = auth disabled)
};
