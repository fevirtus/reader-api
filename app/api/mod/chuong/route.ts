import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import connectToMongoDB from "@/lib/mongoose"
import { Chapter } from "@/lib/models/chapter"
import { prisma } from "@/lib/prisma"

function toNullableNumber(value: any): number | null {
    if (value === null || value === undefined || value === "") return null
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
}

export async function GET(req: Request) {
    const { searchParams } = new URL(req.url)
    const novelId = searchParams.get("novelId")
    const page = parseInt(searchParams.get("page") || "1")
    const limit = parseInt(searchParams.get("limit") || "20")

    if (!novelId) {
        return NextResponse.json({ error: "novelId is required" }, { status: 400 })
    }

    try {
        await connectToMongoDB()
        const skip = (page - 1) * limit

        const [chapters, totalChapters] = await Promise.all([
            Chapter.find({ novelId })
                .sort({ number: 1 })
                .skip(skip)
                .limit(limit)
                .select("-content"),
            Chapter.countDocuments({ novelId })
        ])

        return NextResponse.json({
            chapters,
            totalChapters,
            totalPages: Math.ceil(totalChapters / limit),
            currentPage: page
        })
    } catch (error) {
        console.error("GET Chapter Error:", error)
        return NextResponse.json({ error: "Failed to fetch chapters" }, { status: 500 })
    }
}

export async function POST(req: Request) {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        const data = await req.json()
        const { novelId, number, title, content, volumeNumber, volumeTitle, volumeChapterNumber } = data

        // Xác minh truyện thuộc về Mod này
        const novel = await prisma.novel.findFirst({
            where: { id: novelId, uploaderId: session.user.id },
        })

        if (!novel) {
            return NextResponse.json({ error: "Truyện không tồn tại hoặc không đủ quyền" }, { status: 403 })
        }

        await connectToMongoDB()

        // Kiểm tra chương đã tồn tại
        const existingChapter = await Chapter.findOne({ novelId, number })
        if (existingChapter) {
            return NextResponse.json({ error: "Chương này đã tồn tại" }, { status: 400 })
        }

        const newChapter = await Chapter.create({
            novelId,
            number,
            volumeNumber: toNullableNumber(volumeNumber),
            volumeTitle: typeof volumeTitle === "string" && volumeTitle.trim().length > 0 ? volumeTitle.trim() : null,
            volumeChapterNumber: toNullableNumber(volumeChapterNumber),
            title,
            content,
        })

        // Cập nhật số chương trong table PostgreSQL, tự động đếm lại
        const totalChapters = await Chapter.countDocuments({ novelId })
        await prisma.novel.update({
            where: { id: novelId },
            data: { totalChapters },
        })

        return NextResponse.json(newChapter, { status: 201 })
    } catch (error) {
        console.error("POST Chapter Error:", error)
        return NextResponse.json({ error: "Failed to create chapter" }, { status: 500 })
    }
}

export async function PUT(req: Request) {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        const data = await req.json()
        const { id, novelId, number, title, content, volumeNumber, volumeTitle, volumeChapterNumber } = data

        // Xác minh truyện thuộc về Mod này
        const novel = await prisma.novel.findFirst({
            where: { id: novelId, uploaderId: session.user.id },
        })

        if (!novel) {
            return NextResponse.json({ error: "Truyện không tồn tại hoặc không đủ quyền" }, { status: 403 })
        }

        await connectToMongoDB()

        const updatedChapter = await Chapter.findOneAndUpdate(
            { _id: id, novelId },
            {
                number,
                title,
                content,
                volumeNumber: toNullableNumber(volumeNumber),
                volumeTitle: typeof volumeTitle === "string" && volumeTitle.trim().length > 0 ? volumeTitle.trim() : null,
                volumeChapterNumber: toNullableNumber(volumeChapterNumber),
            },
            { new: true }
        )

        if (!updatedChapter) {
            return NextResponse.json({ error: "Không tìm thấy chương" }, { status: 404 })
        }

        return NextResponse.json(updatedChapter)
    } catch (error) {
        console.error("PUT Chapter Error:", error)
        return NextResponse.json({ error: "Failed to update chapter" }, { status: 500 })
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
        const novelId = url.searchParams.get("novelId")

        if (!id || !novelId) {
            return NextResponse.json({ error: "Thiếu ID chương hoặc ID truyện" }, { status: 400 })
        }

        // Xác minh truyện thuộc về Mod này
        const novel = await prisma.novel.findFirst({
            where: { id: novelId, uploaderId: session.user.id },
        })

        if (!novel) {
            return NextResponse.json({ error: "Truyện không tồn tại hoặc không đủ quyền" }, { status: 403 })
        }

        await connectToMongoDB()

        const deletedChapter = await Chapter.findOneAndDelete({ _id: id, novelId })

        if (!deletedChapter) {
            return NextResponse.json({ error: "Không tìm thấy chương" }, { status: 404 })
        }

        // Cập nhật lại số lượng chương trong Postgres
        const totalChapters = await Chapter.countDocuments({ novelId })
        await prisma.novel.update({
            where: { id: novelId },
            data: { totalChapters },
        })

        return NextResponse.json({ message: "Đã xóa chương thành công" })
    } catch (error) {
        console.error("DELETE Chapter Error:", error)
        return NextResponse.json({ error: "Failed to delete chapter" }, { status: 500 })
    }
}
