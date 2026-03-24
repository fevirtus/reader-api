import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"

export async function GET(req: Request, { params }: { params: Promise<{ id: string }> }) {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    const { id: novelId } = await params

    try {
        const novel = await prisma.novel.findUnique({
            where: { id: novelId },
            select: { trashWords: true, uploaderId: true }
        })

        if (!novel) {
            return NextResponse.json({ error: "Not found" }, { status: 404 })
        }

        if (session.user.role !== "ADMIN" && novel.uploaderId !== session.user.id) {
            return NextResponse.json({ error: "Forbidden" }, { status: 403 })
        }

        return NextResponse.json({ trashWords: novel.trashWords })
    } catch (error) {
        console.error("GET Trash Words Error:", error)
        return NextResponse.json({ error: "Lỗi Server" }, { status: 500 })
    }
}

export async function PUT(req: Request, { params }: { params: Promise<{ id: string }> }) {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    const { id: novelId } = await params

    try {
        const novel = await prisma.novel.findUnique({
            where: { id: novelId },
            select: { id: true, uploaderId: true }
        })

        if (!novel) return NextResponse.json({ error: "Not found" }, { status: 404 })

        if (session.user.role !== "ADMIN" && novel.uploaderId !== session.user.id) {
            return NextResponse.json({ error: "Forbidden" }, { status: 403 })
        }

        const body = await req.json()
        const { trashWords } = body

        if (!Array.isArray(trashWords)) {
            return NextResponse.json({ error: "Mảng từ rác không hợp lệ" }, { status: 400 })
        }

        const updated = await prisma.novel.update({
            where: { id: novelId },
            data: { trashWords }
        })

        return NextResponse.json({ success: true, trashWords: updated.trashWords })
    } catch (error) {
        console.error("PUT Trash Words Error:", error)
        return NextResponse.json({ error: "Lỗi Server" }, { status: 500 })
    }
}
