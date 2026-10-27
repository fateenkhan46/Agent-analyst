/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The browser talks to /api/* on this origin; Next proxies to FastAPI so
  // there is no CORS dance in development and no API host baked into the
  // client bundle. NEXT_PUBLIC_* would be visible to anyone who opens
  // devtools, which is why the backend URL is a server-side rewrite instead.
  async rewrites() {
    const backend = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};
export default nextConfig;
