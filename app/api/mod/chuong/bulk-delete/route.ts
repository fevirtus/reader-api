import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"
import connectToMongoDB from "@/lib/mongoose"
import { Chapter } from "@/lib/models/chapter"

export async function POST(req: Request) {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        const data = await req.json()
        const { novelId, fromNumber, toNumber } = data

        if (!novelId || typeof fromNumber !== "number" || typeof toNumber !== "number") {
            return NextResponse.json({ error: "Dữ liệu không hợp lệ" }, { status: 400 })
        }

        if (fromNumber > toNumber) {
            return NextResponse.json({ error: "Chương bắt đầu không được lớn hơn chương kết thúc" }, { status: 400 })
        }

        // Xác minh truyện thuộc về Mod này (hoặc Admin)
        const novel = await prisma.novel.findUnique({
            where: { id: novelId }
        })

        if (!novel) {
            return NextResponse.json({ error: "Truyện không tồn tại" }, { status: 404 })
        }

        if (session.user.role !== "ADMIN" && novel.uploaderId !== session.user.id) {
            return NextResponse.json({ error: "Bạn không có quyền thao tác trên truyện này" }, { status: 403 })
        }

        await connectToMongoDB()

        // Xóa các chương trong khoảng
        const deleteResult = await Chapter.deleteMany({
            novelId,
            number: { $gte: fromNumber, $lte: toNumber }
        })

        // Cập nhật lại số lượng chương
        const totalChapters = await Chapter.countDocuments({ novelId })
        await prisma.novel.update({
            where: { id: novelId },
            data: { totalChapters },
        })

        return NextResponse.json({
            success: true,
            deletedCount: deleteResult.deletedCount,
            totalChapters
        })
    } catch (error: any) {
        console.error("Bulk Delete Chapters Error:", error)
        return NextResponse.json({ error: "Lỗi hệ thống khi xóa chương: " + error.message }, { status: 500 })
    }
}
