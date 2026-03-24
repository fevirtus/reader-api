import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { uploadBufferToR2 } from "@/lib/r2"

export async function POST(req: Request) {
  try {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    const formData = await req.formData()
    const file = formData.get("file") as File | null

    if (!file) {
      return NextResponse.json({ error: "No file uploaded" }, { status: 400 })
    }

    if (!file.type.startsWith("image/")) {
      return NextResponse.json({ error: "Only image files are allowed" }, { status: 400 })
    }

    const bytes = await file.arrayBuffer()
    const buffer = Buffer.from(bytes)

    const url = await uploadBufferToR2({
      buffer,
      contentType: file.type,
      keyPrefix: "covers/manual",
      fileNameHint: file.name,
    })

    return NextResponse.json({ url })
  } catch (error: any) {
    console.error("Cover upload error:", error)
    return NextResponse.json({ error: error.message || "Failed to upload cover" }, { status: 500 })
  }
}
