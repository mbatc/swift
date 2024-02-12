// @ts-check

const {
  SWIFT_PATH
} = process.env;

/** @type {import('next').NextConfig} */
const nextConfig = {
  basePath: SWIFT_PATH === undefined ? "/swift" : SWIFT_PATH, // TODO: use env to set this?
  productionBrowserSourceMaps: true,
}

module.exports = nextConfig