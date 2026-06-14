import { HttpInterceptorFn } from '@angular/common/http';
import { environment } from '../../environments/environment';

/**
 * HTTP Interceptor that attaches the API key to all outgoing /api/ requests.
 * Only active when environment.apiKey is set (matches API_KEY in server/.env).
 */
export const apiKeyInterceptor: HttpInterceptorFn = (req, next) => {
  const apiKey = environment.apiKey;

  // Skip if no API key configured or request is not to our API
  if (!apiKey || !req.url.includes('/api/')) {
    return next(req);
  }

  const authReq = req.clone({
    setHeaders: { Authorization: `Bearer ${apiKey}` },
  });

  return next(authReq);
};
