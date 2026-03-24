import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"
import { slugify } from "@/lib/utils"

// Get all genres
export async function GET() {
    try {
        const genres = await prisma.genre.findMany({
            orderBy: { name: "asc" }
        })
        return NextResponse.json(genres)
    } catch (error) {
        return NextResponse.json({ error: "Failed to fetch genres" }, { status: 500 })
    }
}

// Admins/Mods can add new genres
export async function POST(req: Request) {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        const data = await req.json()
        const { name, description } = data

        if (!name) {
            return NextResponse.json({ error: "Genre name is required" }, { status: 400 })
        }

        const slug = slugify(name)

        const newGenre = await prisma.genre.create({
            data: { name, slug, description }
        })

        return NextResponse.json(newGenre, { status: 201 })
    } catch (error: any) {
        if (error.code === 'P2002') {
            return NextResponse.json({ error: "Thể loại này đã tồn tại" }, { status: 400 })
        }
        return NextResponse.json({ error: "Failed to create genre" }, { status: 500 })
    }
}

export async function DELETE(req: Request) {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        const url = new URL(req.url)
        const id = url.searchParams.get("id")

        if (!id) {
            return NextResponse.json({ error: "Thiếu ID thể loại" }, { status: 400 })
        }

        await prisma.genre.delete({
            where: { id }
        })

        return NextResponse.json({ message: "Đã xóa thể loại thành công" })
    } catch (error) {
        return NextResponse.json({ error: "Lỗi khi xóa thể loại" }, { status: 500 })
    }
}
