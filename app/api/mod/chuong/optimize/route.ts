import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import connectToMongoDB from "@/lib/mongoose"
import { Chapter } from "@/lib/models/chapter"
import { prisma } from "@/lib/prisma"

export async function PUT(req: Request) {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        const body = await req.json()
        const { novelId, updates } = body

        if (!novelId || !updates || !Array.isArray(updates)) {
            return NextResponse.json({ error: "Tham số không hợp lệ" }, { status: 400 })
        }

        const novel = await prisma.novel.findUnique({
            where: { id: novelId },
            select: { id: true, uploaderId: true }
        })

        if (!novel) {
            return NextResponse.json({ error: "Không tìm thấy truyện" }, { status: 404 })
        }

        if (session.user.role !== "ADMIN" && novel.uploaderId !== session.user.id) {
            return NextResponse.json({ error: "Forbidden" }, { status: 403 })
        }

        const validUpdates = updates.filter((update: any) =>
            update &&
            typeof update.id === "string" &&
            typeof update.number === "number" &&
            typeof update.title === "string"
        )

        if (validUpdates.length === 0) {
            return NextResponse.json({ message: "Không có thay đổi nào" }, { status: 200 })
        }

        await connectToMongoDB()

        // Prepare bulk operations for mongoose
        const bulkOps = validUpdates.map((update: any) => ({
            updateOne: {
                filter: { _id: update.id, novelId: novelId },
                update: {
                    $set: {
                        number: update.number,
                        title: update.title
                    }
                }
            }
        }))

        if (bulkOps.length === 0) {
            return NextResponse.json({ message: "Không có thay đổi nào" }, { status: 200 })
        }

        const result = await Chapter.bulkWrite(bulkOps)

        return NextResponse.json({
            message: "Cập nhật thành công",
            matchedCount: result.matchedCount,
            modifiedCount: result.modifiedCount
        }, { status: 200 })

    } catch (error: any) {
        console.error("Bulk optimize error:", error)
        return NextResponse.json({ error: "Lỗi cập nhật hàng loạt", details: error.message }, { status: 500 })
    }
}
