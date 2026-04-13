import { next } from '@vercel/functions';

/** Edge middleware for HTML shell routes only (see app.py _cache_shell_pages). */
export const config = {
  runtime: 'edge',
  matcher: ['/', '/register', '/account', '/admin'],
};

export default function middleware() {
  return next();
}
