import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"

export async function GET() {
  const session = await getServerSession(authOptions)
  if (!session?.user?.id) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
  }

  try {
    const settings = await prisma.userSetting.findUnique({
      where: { userId: session.user.id }
    })
    
    return NextResponse.json(settings || {})
  } catch (error) {
    console.error("GET User Settings Error", error)
    return NextResponse.json({ error: "Internal Server Error" }, { status: 500 })
  }
}

export async function POST(req: Request) {
  const session = await getServerSession(authOptions)
  if (!session?.user?.id) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
  }

  try {
    const body = await req.json()
    const { fontSize, lineHeight, letterSpacing, fontFamily } = body

    const updateData: any = {}
    if (fontSize !== undefined) updateData.fontSize = Number(fontSize)
    if (lineHeight !== undefined) updateData.lineHeight = Number(lineHeight)
    if (letterSpacing !== undefined) updateData.letterSpacing = Number(letterSpacing)
    if (fontFamily !== undefined) updateData.fontFamily = String(fontFamily)

    const createData = {
      userId: session.user.id,
      fontSize: fontSize !== undefined ? Number(fontSize) : 18,
      lineHeight: lineHeight !== undefined ? Number(lineHeight) : 1.8,
      letterSpacing: letterSpacing !== undefined ? Number(letterSpacing) : 0,
      fontFamily: fontFamily !== undefined ? String(fontFamily) : "font-serif",
    }

    const settings = await prisma.userSetting.upsert({
      where: { userId: session.user.id },
      update: updateData,
      create: createData
    })

    return NextResponse.json(settings)
  } catch (error) {
    console.error("POST User Settings Error", error)
    return NextResponse.json({ error: "Internal Server Error" }, { status: 500 })
  }
}
