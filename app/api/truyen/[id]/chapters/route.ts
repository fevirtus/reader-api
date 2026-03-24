import { NextResponse } from "next/server"
import connectToMongoDB from "@/lib/mongoose"
import { Chapter } from "@/lib/models/chapter"

export async function GET(
  req: Request,
  { params }: { params: Promise<{ id: string }> } // `id` is the `novel.id`
) {
  try {
    const { id: novelId } = await params
    
    const { searchParams } = new URL(req.url)
    const page = parseInt(searchParams.get("page") || "1", 10)
    const limit = parseInt(searchParams.get("limit") || "100", 10)

    await connectToMongoDB()

    const skip = (page - 1) * limit

    const [chapters, totalChapters] = await Promise.all([
      Chapter.find({ novelId })
        .sort({ number: 1 })
        .skip(skip)
        .limit(limit)
        .select("number title createdAt volumeNumber volumeTitle volumeChapterNumber") // don't return content
        .lean(),
      Chapter.countDocuments({ novelId })
    ])

    return NextResponse.json({
      chapters: chapters.map(c => ({
        id: c._id.toString(),
        number: c.number,
        title: c.title,
        volumeNumber: (c as any).volumeNumber ?? null,
        volumeTitle: (c as any).volumeTitle ?? null,
        volumeChapterNumber: (c as any).volumeChapterNumber ?? null,
        createdAt: (c.createdAt as Date).toISOString()
      })),
      totalChapters,
      totalPages: Math.ceil(totalChapters / limit),
      currentPage: page
    })

  } catch (error: any) {
    console.error("Fetch novel chapters error:", error)
    return NextResponse.json(
      { error: "Không thể lấy danh sách chương" },
      { status: 500 }
    )
  }
}
