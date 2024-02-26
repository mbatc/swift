// @ts-check

const {
  NEXT_SWIFT_BASE_PATH
} = process.env;

/** @type {import('next').NextConfig} */
const nextConfig = {
  basePath: NEXT_SWIFT_BASE_PATH === undefined ? "/swift" : NEXT_SWIFT_BASE_PATH,
  productionBrowserSourceMaps: true,
}

module.exports = nextConfig
