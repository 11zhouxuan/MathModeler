import type { NextConfig } from 'next';

const isDevelopment = process.env.NODE_ENV === 'development';

const MINUTES = 1000 * 60;

const nextConfig: NextConfig = {
  // Production: static export -> `out/`, served by the FastAPI portal as a SPA.
  // Development: regular Next dev server with API rewrites to the local portal.
  output: isDevelopment ? undefined : 'export',
  trailingSlash: false,
  images: { unoptimized: true },
  allowedDevOrigins: ['127.0.0.1', 'localhost'],

  // Allow long-lived SSE streams through the dev proxy without timeout.
  experimental: {
    proxyTimeout: 10 * MINUTES,
  },


  // rewrites only apply when output is not 'export' (i.e. dev). Proxy the portal
  // API (login + chat SSE) to the local FastAPI portal on :8090.
  ...(isDevelopment && {
    rewrites: async () => {
      const portalUrl = process.env.PORTAL_URL || 'http://127.0.0.1:8090';
      return [
        { source: '/api/:path*', destination: `${portalUrl}/api/:path*` },
        { source: '/healthz', destination: `${portalUrl}/healthz` },
      ];
    },
  }),
};

export default nextConfig;
