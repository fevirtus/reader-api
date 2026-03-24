import { NextResponse } from "next/server"
import { prisma } from "@/lib/prisma"
import { OAuth2Client } from "google-auth-library"
import { SignJWT, importPKCS8, generateKeyPair } from "jose"
import * as crypto from "crypto"

const googleClient = new OAuth2Client(process.env.GOOGLE_CLIENT_ID)

function generateTokens(userId: string) {
  const secret = process.env.MOBILE_JWT_SECRET || process.env.NEXTAUTH_SECRET || ""
  const key = crypto.createHmac("sha256", secret)
  const payload = Buffer.from(JSON.stringify({ sub: userId, iat: Math.floor(Date.now() / 1000) })).toString("base64url")
  const header = Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString("base64url")
  const sig = key.update(`${header}.${payload}`).digest("base64url")
  const accessToken = `${header}.${payload}.${sig}`

  // refresh token: random 40-byte hex, stored hashed in DB if needed; for now return raw
  const refreshToken = crypto.randomBytes(40).toString("hex")
  return { accessToken, refreshToken }
}

export async function POST(req: Request) {
  try {
    const body = await req.json()
    const { googleIdToken } = body

    if (!googleIdToken || typeof googleIdToken !== "string") {
      return NextResponse.json({ error: "googleIdToken is required" }, { status: 400 })
    }

    // Verify the Google ID token
    let ticket
    try {
      ticket = await googleClient.verifyIdToken({
        idToken: googleIdToken,
        audience: process.env.GOOGLE_CLIENT_ID,
      })
    } catch {
      return NextResponse.json({ error: "Invalid Google token" }, { status: 401 })
    }

    const payload = ticket.getPayload()
    if (!payload?.email) {
      return NextResponse.json({ error: "Unable to extract email from token" }, { status: 401 })
    }

    const { email, name, picture, sub: googleSub } = payload

    // Upsert user — match NextAuth behaviour
    let user = await prisma.user.findUnique({ where: { email } })
    if (!user) {
      user = await prisma.user.create({
        data: {
          email,
          name: name ?? null,
          image: picture ?? null,
          emailVerified: new Date(),
        },
      })
    }

    // Upsert Google Account link
    const existingAccount = await prisma.account.findUnique({
      where: { provider_providerAccountId: { provider: "google", providerAccountId: googleSub } },
    })
    if (!existingAccount) {
      await prisma.account.create({
        data: {
          userId: user.id,
          type: "oauth",
          provider: "google",
          providerAccountId: googleSub,
        },
      })
    }

    const { accessToken, refreshToken } = generateTokens(user.id)

    return NextResponse.json({
      accessToken,
      refreshToken,
      expiresIn: 3600,
      user: {
        id: user.id,
        email: user.email,
        name: user.name,
        image: user.image,
        role: user.role,
      },
    })
  } catch (error) {
    console.error("Mobile login error:", error)
    return NextResponse.json({ error: "Internal Server Error" }, { status: 500 })
  }
}
