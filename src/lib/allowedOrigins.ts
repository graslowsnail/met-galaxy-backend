import dotenv from "dotenv";

dotenv.config();

const DEVELOPMENT_ORIGINS = [
  "http://localhost:3000",
  "http://localhost:3001",
  "http://127.0.0.1:3000",
];

const configuredOrigins = process.env.CORS_ORIGINS
  ?.split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);

export const allowedOrigins = process.env.NODE_ENV === "development"
  ? DEVELOPMENT_ORIGINS
  : configuredOrigins?.length
    ? configuredOrigins
    : ["https://openmetropolitan.com"];
