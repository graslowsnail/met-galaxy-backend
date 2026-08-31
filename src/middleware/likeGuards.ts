import type { NextFunction, Request, Response } from "express";
import { ipKeyGenerator, rateLimit } from "express-rate-limit";
import { allowedOrigins } from "../lib/allowedOrigins.js";

export const CLIENT_HEADER = "X-Met-Galaxy-Client";
const CLIENT_HEADER_VALUE = "web";

const WINDOW_MS = 10 * 60 * 1000;
const MAX_MUTATIONS_PER_IP = 100;
const MAX_MUTATIONS_PER_VOTER = 40;

const logBlocked = (req: Request, reason: string) => {
  console.warn(
    `🚫 [LIKE-GUARD] Blocked ${req.method} ${req.originalUrl} | reason=${reason} | ip=${req.ip ?? "unknown"} | origin=${req.get("origin") ?? "none"}`
  );
};

const ipKey = (req: Request) => `ip:${ipKeyGenerator(req.ip ?? "unknown")}`;

const tooManyRequests = (scope: string) => (req: Request, res: Response) => {
  logBlocked(req, `rate-limit:${scope}`);
  res.status(429).json({
    success: false,
    error: "Too many like requests. Please slow down and try again in a few minutes.",
  });
};

export const requireTrustedOrigin = (req: Request, res: Response, next: NextFunction) => {
  const origin = req.get("origin");
  if (!origin || !allowedOrigins.includes(origin)) {
    logBlocked(req, "origin");
    return res.status(403).json({ success: false, error: "Request origin is not allowed" });
  }

  // Sent by browsers on direct navigation / address-bar requests, never by a real fetch from our app.
  if (req.get("sec-fetch-site") === "none") {
    logBlocked(req, "sec-fetch-site");
    return res.status(403).json({ success: false, error: "Request origin is not allowed" });
  }

  // A custom header cannot be set cross-origin without passing a CORS preflight first.
  if (req.get(CLIENT_HEADER) !== CLIENT_HEADER_VALUE) {
    logBlocked(req, "client-header");
    return res.status(403).json({ success: false, error: "Request origin is not allowed" });
  }

  next();
};

export const likeIpRateLimit = rateLimit({
  windowMs: WINDOW_MS,
  limit: MAX_MUTATIONS_PER_IP,
  standardHeaders: "draft-7",
  legacyHeaders: false,
  keyGenerator: ipKey,
  handler: tooManyRequests("ip"),
});

export const likeVoterRateLimit = rateLimit({
  windowMs: WINDOW_MS,
  limit: MAX_MUTATIONS_PER_VOTER,
  standardHeaders: "draft-7",
  legacyHeaders: false,
  keyGenerator: (req) => {
    const voterId = (req.body as { voterId?: unknown } | undefined)?.voterId;
    return typeof voterId === "string" && voterId.length > 0 ? `voter:${voterId}` : ipKey(req);
  },
  handler: tooManyRequests("voter"),
});
