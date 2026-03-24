import { DeleteObjectCommand, PutObjectCommand, S3Client } from "@aws-sdk/client-s3"
import path from "path"

type UploadToR2Options = {
  buffer: Buffer
  contentType?: string | null
  keyPrefix?: string
  fileNameHint?: string
}

let cachedClient: S3Client | null = null

function requiredEnv(name: string): string {
  const value = process.env[name]
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`)
  }
  return value
}

function optionalEnv(name: string): string | null {
  const value = process.env[name]
  return value ? value : null
}

function getR2Config() {
  const accountId = requiredEnv("R2_ACCOUNT_ID")
  const accessKeyId = requiredEnv("R2_ACCESS_KEY_ID")
  const secretAccessKey = requiredEnv("R2_SECRET_ACCESS_KEY")
  const bucket = requiredEnv("R2_BUCKET_NAME")
  const publicBaseUrl = requiredEnv("R2_PUBLIC_BASE_URL")

  return {
    accountId,
    accessKeyId,
    secretAccessKey,
    bucket,
    publicBaseUrl: publicBaseUrl.replace(/\/+$/, ""),
  }
}

function getR2Client(): S3Client {
  if (cachedClient) return cachedClient

  const { accountId, accessKeyId, secretAccessKey } = getR2Config()

  cachedClient = new S3Client({
    region: "auto",
    endpoint: `https://${accountId}.r2.cloudflarestorage.com`,
    credentials: {
      accessKeyId,
      secretAccessKey,
    },
  })

  return cachedClient
}

function extensionFromMimeType(mimeType: string | null | undefined): string {
  if (!mimeType) return ".jpg"
  const normalized = mimeType.toLowerCase()
  if (normalized.includes("png")) return ".png"
  if (normalized.includes("webp")) return ".webp"
  if (normalized.includes("gif")) return ".gif"
  if (normalized.includes("avif")) return ".avif"
  if (normalized.includes("jpeg") || normalized.includes("jpg")) return ".jpg"
  return ".jpg"
}

function extensionFromHint(fileNameHint?: string): string {
  if (!fileNameHint) return ""
  const ext = path.extname(fileNameHint).toLowerCase()
  if (!ext) return ""
  if (!/^\.[a-z0-9]{1,8}$/.test(ext)) return ""
  return ext
}

export async function uploadBufferToR2(options: UploadToR2Options): Promise<string> {
  const client = getR2Client()
  const { bucket, publicBaseUrl } = getR2Config()

  const keyPrefix = (options.keyPrefix || "covers").replace(/^\/+|\/+$/g, "")
  const ext = extensionFromHint(options.fileNameHint) || extensionFromMimeType(options.contentType)
  const key = `${keyPrefix}/${Date.now()}-${Math.round(Math.random() * 1e9)}${ext}`

  await client.send(
    new PutObjectCommand({
      Bucket: bucket,
      Key: key,
      Body: options.buffer,
      ContentType: options.contentType || "application/octet-stream",
    })
  )

  return `${publicBaseUrl}/${key}`
}

export function getR2ObjectKeyFromUrl(url: string | null | undefined): string | null {
  if (!url) return null

  const publicBaseUrl = optionalEnv("R2_PUBLIC_BASE_URL")?.replace(/\/+$/, "")
  if (!publicBaseUrl) return null

  let baseUrl: URL
  let fileUrl: URL

  try {
    baseUrl = new URL(publicBaseUrl)
    fileUrl = new URL(url)
  } catch {
    return null
  }

  if (baseUrl.origin !== fileUrl.origin) return null

  const basePath = baseUrl.pathname.replace(/\/+$/, "")
  if (!fileUrl.pathname.startsWith(basePath)) return null

  const relativePath = fileUrl.pathname.slice(basePath.length).replace(/^\/+/, "")
  if (!relativePath) return null

  return decodeURIComponent(relativePath)
}

export async function deleteR2ObjectByUrl(url: string | null | undefined): Promise<boolean> {
  const key = getR2ObjectKeyFromUrl(url)
  if (!key) return false

  const client = getR2Client()
  const { bucket } = getR2Config()

  await client.send(
    new DeleteObjectCommand({
      Bucket: bucket,
      Key: key,
    })
  )

  return true
}
