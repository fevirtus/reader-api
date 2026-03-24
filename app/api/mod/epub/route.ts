import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"
import connectToMongoDB from "@/lib/mongoose"
import { Chapter } from "@/lib/models/chapter"
import path from "path"
import os from "os"
import { promises as fs } from "fs"
import { convert } from "html-to-text"
import { slugify } from "@/lib/utils"
import { deleteR2ObjectByUrl, uploadBufferToR2 } from "@/lib/r2"

export const maxDuration = 900

type SplitMode = "toc" | "regex"
type SeriesMode = "none" | "existing" | "new"

type UserRole = "USER" | "MOD" | "ADMIN"

interface EpubSection {
    sourceTitle: string
    content: string
}

interface ParsedChapter {
    title: string
    content: string
    detectedChapterNumber: number | null
    finalNumber?: number
    volumeNumber: number | null
    volumeTitle: string | null
    volumeChapterNumber: number | null
    isPlaceholder?: boolean
}

interface EpubCoverAsset {
    buffer: Buffer | null
    mimeType: string | null
    sourceId: string | null
}

type EpubCtor = new (epubfile: string, imagewebroot: string, chapterwebroot: string) => any

function sanitizeEpubNavMapBranch(branch: any): any {
    if (Array.isArray(branch)) {
        branch.forEach((item) => sanitizeEpubNavMapBranch(item))
        return branch
    }

    if (!branch || typeof branch !== "object") {
        return branch
    }

    if ("navLabel" in branch) {
        const navLabel = (branch as any).navLabel

        if (typeof navLabel === "string") {
            ;(branch as any).navLabel = navLabel
        } else if (navLabel && typeof navLabel === "object") {
            if (typeof navLabel.text === "string") {
                ;(branch as any).navLabel = navLabel.text
            } else if (navLabel.text !== undefined && navLabel.text !== null) {
                ;(branch as any).navLabel = String(navLabel.text)
            } else {
                ;(branch as any).navLabel = ""
            }
        } else if (navLabel === null || navLabel === undefined) {
            ;(branch as any).navLabel = ""
        } else {
            ;(branch as any).navLabel = String(navLabel)
        }
    }

    if ((branch as any).navPoint) {
        sanitizeEpubNavMapBranch((branch as any).navPoint)
    }

    return branch
}

function getPatchedEpubCtor(): EpubCtor {
    const loaded = require("epub2")
    const EPub = (loaded?.EPub || loaded) as EpubCtor
    const proto = (EPub as any)?.prototype

    if (proto && !proto.__readerSafeNavLabelPatchApplied && typeof proto.walkNavMap === "function") {
        const originalWalkNavMap = proto.walkNavMap

        proto.walkNavMap = function patchedWalkNavMap(branch: any, ...args: any[]) {
            const safeBranch = sanitizeEpubNavMapBranch(branch)
            return originalWalkNavMap.call(this, safeBranch, ...args)
        }

        proto.__readerSafeNavLabelPatchApplied = true
    }

    return EPub
}

function isRequestAbortedError(error: unknown): boolean {
    if (!error) return false

    const candidate = error as { code?: unknown; message?: unknown; name?: unknown }
    const code = typeof candidate.code === "string" ? candidate.code.toUpperCase() : ""
    const message = typeof candidate.message === "string" ? candidate.message.toLowerCase() : ""
    const name = typeof candidate.name === "string" ? candidate.name.toLowerCase() : ""

    return (
        code === "ECONNRESET" ||
        code === "ABORT_ERR" ||
        message.includes("aborted") ||
        message.includes("connection reset") ||
        name.includes("abort")
    )
}

const CHAPTER_REGEX_PRESETS: Record<string, string> = {
    vi_chuong_hoi: "^(?:Chương|Hồi|Tiết|Phần|Thứ|Quyển|Ch\\.)\\s*\\d+(?:\\.\\d+)?[^\\n]*$",
    mix_chapter: "^(?:Chương|Hồi|Tiết|Phần|Thứ|Quyển|Chapter|Ch\\.)\\s*\\d+(?:\\.\\d+)?[^\\n]*$",
    numeric_only: "^\\d+(?:\\.\\d+)?\\s*[\\.\\:\\-\\]\\)]?(?:\\s+|$)[^\\n]*$",
}

const LEGACY_CHAPTER_REGEX_PRESET_ALIASES: Record<string, keyof typeof CHAPTER_REGEX_PRESETS> = {
    vi_chuong: "vi_chuong_hoi",
    en_chapter: "mix_chapter",
    bracket_chapter: "mix_chapter",
}

const NOISE_TITLE_REGEX = /^(?:mục lục|table of contents|toc|cover|bìa|copyright)$/i

const SIMPLE_CHAPTER_TITLE_REGEX = /^(?:ch(?:ương|apter)?|ch\.|hồi|tiết|phần|thứ|quyển)\s*\d+(?:\.\d+)?\s*:?$/i
const GENERIC_SECTION_TITLE_REGEX = /^(?:m[uụ]c|section|sec\.?|part|ph[aầ]n)\s*[0-9ivxlcdm]+$/i
const WEAK_CHAPTER_TOC_TITLE_REGEX = /^(?:ch(?:ương|apter)?|ch\.|hồi|tiết|phần|thứ|quyển)\s*\d+(?:\.\d+)?\s*[:\-–—]\s*(?:m[uụ]c|section|sec\.?|part)\s*[0-9ivxlcdm]+$/i
const CHAPTER_HEADING_LINE_REGEX = /^(?:\[?\s*)?(?:ch(?:ương|apter)?|ch\.|hồi|tiết|phần|thứ|quyển)?\s*([0-9]+)(?:\.[0-9]+)?(?:\s*\]?)*(?:(?:\s*[:\-–—\.]\s*|\s+)(.+))?$/i
const GENERIC_GENRE_TOKENS = new Set([
    "book",
    "books",
    "ebook",
    "fiction",
    "literature",
    "novel",
    "story",
    "truyen",
    "tiểu thuyết",
])

function isWeakTOCTitle(title: string): boolean {
    const normalized = title.trim()
    return (
        SIMPLE_CHAPTER_TITLE_REGEX.test(normalized) ||
        GENERIC_SECTION_TITLE_REGEX.test(normalized) ||
        WEAK_CHAPTER_TOC_TITLE_REGEX.test(normalized)
    )
}

function pickChapterHeadingFromContent(content: string): {
    heading: string
    chapterNumber: number | null
    consumedLineIndexes: number[]
} | null {
    const lines = content.split(/\r?\n/)
    const nonEmptyIndexes = lines
        .map((line, index) => ({ line: line.trim(), index }))
        .filter((item) => item.line.length > 0)

    for (let i = 0; i < nonEmptyIndexes.length && i < 12; i++) {
        const current = nonEmptyIndexes[i]
        const headingLine = current.line.replace(/\s+/g, " ").trim()

        if (!headingLine || headingLine.length > 180) continue
        if (NOISE_TITLE_REGEX.test(headingLine) || isVolumeHeading(headingLine)) continue

        const matched = headingLine.match(CHAPTER_HEADING_LINE_REGEX)
        if (!matched) continue

        const parsed = Number(matched[1])
        const chapterNumber = Number.isInteger(parsed) && parsed > 0 && parsed <= 50000 ? parsed : null
        const consumedLineIndexes = [current.index]

        let heading = headingLine
        const trailingTitle = (matched[2] || "").trim()
        if (!trailingTitle) {
            const next = nonEmptyIndexes[i + 1]
            if (next) {
                const subtitle = next.line.replace(/\s+/g, " ").trim()
                if (
                    subtitle.length > 0 &&
                    subtitle.length <= 120 &&
                    !NOISE_TITLE_REGEX.test(subtitle) &&
                    !isVolumeHeading(subtitle) &&
                    !CHAPTER_HEADING_LINE_REGEX.test(subtitle)
                ) {
                    heading = `${headingLine.replace(/[:\-–—\.\s]+$/g, "")}: ${subtitle}`
                    consumedLineIndexes.push(next.index)
                }
            }
        }

        return { heading, chapterNumber, consumedLineIndexes }
    }

    return null
}

function normalizeMetaText(value: any, fallback: string) {
    if (typeof value === "string" && value.trim().length > 0) return value.trim()
    if (Array.isArray(value)) {
        const first = value.find((v) => typeof v === "string" && v.trim().length > 0)
        if (first) return first.trim()
    }
    return fallback
}

function canReplaceNovelByRole(userRole: UserRole, userId: string, novel: { uploaderId: string | null }): boolean {
    if (userRole === "ADMIN") return true
    return novel.uploaderId === userId || novel.uploaderId === null
}

function collectTextValues(input: any, bucket: string[]) {
    if (input === null || input === undefined) return

    if (typeof input === "string") {
        bucket.push(input)
        return
    }

    if (typeof input === "number" || typeof input === "boolean") {
        bucket.push(String(input))
        return
    }

    if (Array.isArray(input)) {
        input.forEach((item) => collectTextValues(item, bucket))
        return
    }

    if (typeof input === "object") {
        Object.values(input).forEach((value) => collectTextValues(value, bucket))
    }
}

function normalizeGenreCandidate(name: string): string {
    return name
        .replace(/\s+/g, " ")
        .replace(/^[\s\-–—:;,.\/|]+|[\s\-–—:;,.\/|]+$/g, "")
        .trim()
}

function extractGenreCandidatesFromMetadata(metadata: any): string[] {
    if (!metadata || typeof metadata !== "object") return []

    const rawValues: string[] = []
    const keys = Object.keys(metadata)
    const candidateKeys = keys.filter((key) => /subject|genre|tag|category/i.test(key))

    for (const key of candidateKeys) {
        collectTextValues((metadata as Record<string, any>)[key], rawValues)
    }

    const uniqueNames: string[] = []
    const seen = new Set<string>()

    for (const raw of rawValues) {
        const chunks = raw
            .split(/[,;|/\n]+/)
            .map((chunk) => normalizeGenreCandidate(chunk))
            .filter(Boolean)

        for (const name of chunks) {
            const normalized = name.toLowerCase()
            if (name.length < 2 || name.length > 80) continue
            if (GENERIC_GENRE_TOKENS.has(normalized)) continue
            if (seen.has(normalized)) continue

            seen.add(normalized)
            uniqueNames.push(name)

            if (uniqueNames.length >= 12) {
                return uniqueNames
            }
        }
    }

    return uniqueNames
}

async function resolveGenreIdsFromNames(genreNames: string[], createIfMissing: boolean): Promise<string[]> {
    const ids: string[] = []

    for (const genreName of genreNames) {
        const existing = await prisma.genre.findFirst({
            where: { name: { equals: genreName, mode: "insensitive" } },
            select: { id: true },
        })

        if (existing) {
            ids.push(existing.id)
            continue
        }

        if (!createIfMissing) continue

        const baseSlug = slugify(genreName) || `genre-${Date.now()}`
        let slug = baseSlug
        let counter = 1

        while (await prisma.genre.findUnique({ where: { slug } })) {
            slug = `${baseSlug}-${counter}`
            counter += 1
        }

        try {
            const created = await prisma.genre.create({
                data: {
                    name: genreName,
                    slug,
                },
                select: { id: true },
            })
            ids.push(created.id)
        } catch (error: any) {
            if (error?.code === "P2002") {
                const fallback = await prisma.genre.findFirst({
                    where: { name: { equals: genreName, mode: "insensitive" } },
                    select: { id: true },
                })
                if (fallback) {
                    ids.push(fallback.id)
                    continue
                }
            }

            throw error
        }
    }

    return Array.from(new Set(ids))
}

async function findNovelByTitleInsensitive(title: string) {
    return prisma.novel.findFirst({
        where: { title: { equals: title, mode: "insensitive" } },
        orderBy: { updatedAt: "desc" },
        select: {
            id: true,
            title: true,
            slug: true,
            coverUrl: true,
            uploaderId: true,
        },
    })
}

function extractVolumeNumber(title: string): number | null {
    const matched = title.match(/(?:quy[eê]n|vol(?:ume)?|t[aạ]p|book|arc|hồi)\s*([0-9]+)/i)
    if (!matched) return null
    const parsed = Number(matched[1])
    return Number.isFinite(parsed) ? parsed : null
}

function extractChapterNumber(title: string): number | null {
    const matched = title.match(/(?:ch(?:ương|apter)?|ch\.)\s*([0-9]+(?:\.[0-9]+)?)/i)
    if (!matched) return null
    const parsed = Number(matched[1])
    return Number.isFinite(parsed) ? parsed : null
}

function extractStrictChapterNumber(title: string): number | null {
    const number = extractChapterNumber(title)
    if (number === null) return null
    if (!Number.isInteger(number)) return null
    if (number <= 0) return null
    if (number > 50000) return null
    return number
}

function enhanceChapterTitleFromContent(title: string, content: string): { title: string; content: string; detectedChapterNumber: number | null } {
    const lines = content.split(/\r?\n/)
    const firstNonEmptyLineIndex = lines.findIndex((line) => line.trim().length > 0)
    const baseTitle = title.trim()
    const baseDetectedChapterNumber = extractStrictChapterNumber(baseTitle)
    if (firstNonEmptyLineIndex < 0) {
        return {
            title,
            content,
            detectedChapterNumber: baseDetectedChapterNumber,
        }
    }

    const firstLineRaw = lines[firstNonEmptyLineIndex]
    const firstLine = firstLineRaw.trim()
    if (!firstLine) {
        return {
            title,
            content,
            detectedChapterNumber: baseDetectedChapterNumber,
        }
    }

    const detectedHeading = pickChapterHeadingFromContent(content)
    if (detectedHeading) {
        const shouldUseDetectedHeading =
            isWeakTOCTitle(baseTitle) ||
            baseDetectedChapterNumber === null ||
            detectedHeading.chapterNumber === baseDetectedChapterNumber

        if (shouldUseDetectedHeading) {
            const nextLines = [...lines]
            detectedHeading.consumedLineIndexes
                .sort((a, b) => b - a)
                .forEach((index) => {
                    nextLines.splice(index, 1)
                })

            const nextContent = nextLines.join("\n").trim()
            return {
                title: detectedHeading.heading,
                content: nextContent.length > 0 ? nextContent : content,
                detectedChapterNumber: detectedHeading.chapterNumber,
            }
        }
    }

    if (firstLine.length > 140) {
        return {
            title,
            content,
            detectedChapterNumber: baseDetectedChapterNumber,
        }
    }

    const isSimpleBaseTitle = SIMPLE_CHAPTER_TITLE_REGEX.test(baseTitle)

    if (!isSimpleBaseTitle) {
        return {
            title,
            content,
            detectedChapterNumber: baseDetectedChapterNumber,
        }
    }

    let nextTitle = baseTitle

    // Case 1: The first line already contains full chapter heading, use it directly.
    if (/^(?:ch(?:ương|apter)?|ch\.)\s*\d+/i.test(firstLine) && firstLine.length > baseTitle.length + 2) {
        nextTitle = firstLine
    } else if (!SIMPLE_CHAPTER_TITLE_REGEX.test(firstLine) && !isVolumeHeading(firstLine) && !NOISE_TITLE_REGEX.test(firstLine)) {
        // Case 2: TOC title is only "Chương N", subtitle is on next line.
        nextTitle = `${baseTitle.replace(/[:\s]+$/g, "")}: ${firstLine}`
    } else {
        return {
            title,
            content,
            detectedChapterNumber: baseDetectedChapterNumber,
        }
    }

    const newLines = [...lines]
    newLines.splice(firstNonEmptyLineIndex, 1)
    const nextContent = newLines.join("\n").trim()

    return {
        title: nextTitle,
        content: nextContent.length > 0 ? nextContent : content,
        detectedChapterNumber: extractStrictChapterNumber(nextTitle) ?? baseDetectedChapterNumber,
    }
}

function isVolumeHeading(title: string): boolean {
    return /^(?:quy[eê]n|vol(?:ume)?|t[aạ]p|book|arc|hồi)\s*[0-9]+(?:\s*[:-].*)?$/i.test(title.trim())
}

function normalizeSplitMode(value: FormDataEntryValue | null): SplitMode {
    return value === "regex" ? "regex" : "toc"
}

function normalizeSeriesMode(value: FormDataEntryValue | null): SeriesMode {
    if (value === "existing") return "existing"
    if (value === "new") return "new"
    return "none"
}

function readFormText(formData: FormData, key: string): string {
    const value = formData.get(key)
    return typeof value === "string" ? value.trim() : ""
}

async function resolveSeriesIdForEpubImport(options: {
    mode: SeriesMode
    seriesId: string
    seriesName: string
    userRole: "USER" | "MOD" | "ADMIN"
    userId: string
}) {
    if (options.mode === "none") return null

    if (options.mode === "existing") {
        if (!options.seriesId) {
            throw new Error("Thiếu series để thêm vào")
        }

        const targetSeries = await prisma.series.findFirst({
            where: options.userRole === "ADMIN"
                ? { id: options.seriesId }
                : {
                    id: options.seriesId,
                    OR: [
                        { novels: { some: { uploaderId: options.userId } } },
                        { novels: { some: { uploaderId: null } } },
                        { novels: { none: {} } },
                    ],
                },
            select: { id: true },
        })

        if (!targetSeries) {
            throw new Error("Series không tồn tại hoặc không đủ quyền")
        }

        return targetSeries.id
    }

    if (!options.seriesName) {
        throw new Error("Thiếu tên series mới")
    }

    const existed = await prisma.series.findFirst({
        where: { name: { equals: options.seriesName, mode: "insensitive" } },
        select: { id: true },
    })

    if (existed) return existed.id

    const baseSlug = slugify(options.seriesName)
    let slug = baseSlug
    let counter = 1

    while (await prisma.series.findUnique({ where: { slug } })) {
        slug = `${baseSlug}-${counter}`
        counter += 1
    }

    const created = await prisma.series.create({
        data: {
            name: options.seriesName,
            slug,
        },
        select: { id: true },
    })

    return created.id
}

function resolveRegexPattern(formData: FormData): { regexInput: string; regexPreset: string | null } {
    const preset = readFormText(formData, "chapterRegexPreset")
    const custom = readFormText(formData, "chapterRegex")

    if (custom) {
        return { regexInput: custom, regexPreset: preset || "custom" }
    }

    if (preset) {
        const normalizedPreset = (CHAPTER_REGEX_PRESETS[preset] ? preset : LEGACY_CHAPTER_REGEX_PRESET_ALIASES[preset]) || null
        if (normalizedPreset && CHAPTER_REGEX_PRESETS[normalizedPreset]) {
            return { regexInput: CHAPTER_REGEX_PRESETS[normalizedPreset], regexPreset: normalizedPreset }
        }
    }

    return { regexInput: CHAPTER_REGEX_PRESETS.vi_chuong_hoi, regexPreset: "vi_chuong_hoi" }
}

function buildRegexFromInput(regexInput: string): { regex: RegExp; normalized: string } {
    if (!regexInput || regexInput.length > 300) {
        throw new Error("Regex không hợp lệ")
    }

    let pattern = regexInput
    let flags = ""

    const slashWrapped = regexInput.match(/^\/(.+)\/([gimsuy]*)$/)
    if (slashWrapped) {
        pattern = slashWrapped[1]
        flags = slashWrapped[2]
    }

    const flagSet = new Set(flags.split(""))
    flagSet.add("g")
    flagSet.add("m")
    flagSet.add("i")
    const normalizedFlags = Array.from(flagSet).join("")
    const regex = new RegExp(pattern, normalizedFlags)

    return { regex, normalized: `/${pattern}/${normalizedFlags}` }
}

function enrichVolumeMetadata(chapters: Array<{ title: string; content: string }>): ParsedChapter[] {
    let currentVolumeNumber: number | null = null
    let currentVolumeTitle: string | null = null
    let volumeChapterCounter = 0

    return chapters.map((chapter) => {
        const title = chapter.title.trim()

        const explicitVolumeNumber = extractVolumeNumber(title)
        if (explicitVolumeNumber !== null) {
            if (currentVolumeNumber !== explicitVolumeNumber) {
                volumeChapterCounter = 0
            }

            currentVolumeNumber = explicitVolumeNumber
            currentVolumeTitle = isVolumeHeading(title)
                ? title
                : (currentVolumeTitle || `Quyển ${explicitVolumeNumber}`)
        }

        const explicitChapterNumber = extractChapterNumber(title)
        let volumeChapterNumber: number | null = null

        if (currentVolumeNumber !== null) {
            if (explicitChapterNumber !== null) {
                volumeChapterCounter = explicitChapterNumber
            } else {
                volumeChapterCounter += 1
            }
            volumeChapterNumber = volumeChapterCounter
        }

        return {
            title,
            content: chapter.content,
            detectedChapterNumber: extractStrictChapterNumber(title),
            volumeNumber: currentVolumeNumber,
            volumeTitle: currentVolumeTitle,
            volumeChapterNumber,
        }
    })
}

function buildChaptersFromTOCSections(sections: EpubSection[]): ParsedChapter[] {
    const chapters: ParsedChapter[] = []

    let currentVolumeNumber: number | null = null
    let currentVolumeTitle: string | null = null
    let currentVolumeChapterCounter = 0
    let fallbackVolumeCounter = 0

    for (let i = 0; i < sections.length; i++) {
        const section = sections[i]
        const rawTitle = section.sourceTitle || `Chương ${i + 1}`
        const cleanTitle = rawTitle.replace(/\s+/g, " ").trim()
        const cleanContent = section.content.trim()

        if (!cleanContent) continue

        if (isVolumeHeading(cleanTitle)) {
            const extracted = extractVolumeNumber(cleanTitle)
            if (extracted !== null) {
                currentVolumeNumber = extracted
            } else {
                fallbackVolumeCounter += 1
                currentVolumeNumber = fallbackVolumeCounter
            }

            currentVolumeTitle = cleanTitle
            currentVolumeChapterCounter = 0

            if (cleanContent.length <= 240) {
                continue
            }
        }

        if (NOISE_TITLE_REGEX.test(cleanTitle) && cleanContent.length <= 240) {
            continue
        }

        const explicitVolumeFromTitle = extractVolumeNumber(cleanTitle)
        if (explicitVolumeFromTitle !== null) {
            if (currentVolumeNumber !== explicitVolumeFromTitle) {
                currentVolumeChapterCounter = 0
            }
            currentVolumeNumber = explicitVolumeFromTitle
            if (!currentVolumeTitle || isVolumeHeading(cleanTitle)) {
                currentVolumeTitle = `Quyển ${explicitVolumeFromTitle}`
            }
        }

        const enhanced = enhanceChapterTitleFromContent(cleanTitle, cleanContent)
        let volumeChapterNumber: number | null = null
        const detectedChapterNumber = enhanced.detectedChapterNumber
        if (currentVolumeNumber !== null) {
            const explicitChapter = extractChapterNumber(enhanced.title)
            if (explicitChapter !== null) {
                currentVolumeChapterCounter = explicitChapter
            } else {
                currentVolumeChapterCounter += 1
            }
            volumeChapterNumber = currentVolumeChapterCounter
        }

        chapters.push({
            title: enhanced.title,
            content: enhanced.content,
            detectedChapterNumber,
            volumeNumber: currentVolumeNumber,
            volumeTitle: currentVolumeTitle,
            volumeChapterNumber,
        })
    }

    return chapters
}

function buildChaptersFromRegexSections(sections: EpubSection[], regex: RegExp): ParsedChapter[] {
    const combinedText = sections
        .map((section) => section.content.trim())
        .filter(Boolean)
        .join("\n\n")

    const matches = Array.from(combinedText.matchAll(regex))
    if (matches.length === 0) {
        return []
    }

    const parsed: Array<{ title: string; content: string }> = []

    for (let i = 0; i < matches.length; i++) {
        const match = matches[i]
        if (match.index === undefined) continue

        const nextMatch = matches[i + 1]
        const headingRaw = (match[1] || match[0] || "").replace(/\s+/g, " ").trim()
        const sectionStart = match.index + match[0].length
        const sectionEnd = nextMatch?.index ?? combinedText.length
        const body = combinedText.slice(sectionStart, sectionEnd).trim()

        if (!headingRaw || body.length === 0) {
            continue
        }

        const enhanced = enhanceChapterTitleFromContent(headingRaw, body)

        parsed.push({
            title: enhanced.title,
            content: enhanced.content,
        })
    }

    return enrichVolumeMetadata(parsed)
}

function trimLeadingBeforeChapterOne(chapters: ParsedChapter[]): {
    chapters: ParsedChapter[]
    trimmedCount: number
} {
    if (chapters.length === 0) {
        return { chapters, trimmedCount: 0 }
    }

    const firstChapterOneIndex = chapters.findIndex((chapter) => {
        const detected =
            chapter.detectedChapterNumber ??
            extractStrictChapterNumber(chapter.title) ??
            extractChapterNumber(chapter.title)

        return detected === 1
    })

    if (firstChapterOneIndex <= 0) {
        return { chapters, trimmedCount: 0 }
    }

    return {
        chapters: chapters.slice(firstChapterOneIndex),
        trimmedCount: firstChapterOneIndex,
    }
}

function withMissingChapterPlaceholders(chapters: ParsedChapter[]): {
    chapters: ParsedChapter[]
    insertedCount: number
    detectedMax: number
    detectedNumberAssignments: number
} {
    const detectedNumbers = chapters
        .map((chapter) => chapter.detectedChapterNumber)
        .filter((n): n is number => typeof n === "number" && Number.isInteger(n) && n > 0)

    let insertedCount = 0
    let detectedNumberAssignments = 0
    let currentNumber = 0
    const maxDetected = detectedNumbers.length > 0 ? Math.max(...detectedNumbers) : chapters.length
    const normalized: ParsedChapter[] = []
    const MAX_ALLOWED_GAP = 40

    for (const chapter of chapters) {
        const detected = chapter.detectedChapterNumber
        const detectedNumber = typeof detected === "number" ? detected : null
        let canUseDetected =
            detectedNumber !== null &&
            detectedNumber > currentNumber &&
            (currentNumber === 0 || detectedNumber - currentNumber <= MAX_ALLOWED_GAP)

        // Recover from noisy leading TOC entries such as "Mục 1" that shift numbering.
        if (!canUseDetected && detectedNumber !== null && detectedNumber > 0 && detectedNumber <= currentNumber) {
            while (
                normalized.length > 0 &&
                detectedNumber <= currentNumber &&
                normalized[normalized.length - 1].detectedChapterNumber === null &&
                !normalized[normalized.length - 1].isPlaceholder &&
                isWeakTOCTitle(normalized[normalized.length - 1].title)
            ) {
                normalized.pop()
                currentNumber = Math.max(0, currentNumber - 1)
            }

            canUseDetected = detectedNumber > currentNumber && (currentNumber === 0 || detectedNumber - currentNumber <= MAX_ALLOWED_GAP)
        }

        if (canUseDetected && detectedNumber !== null) {
            if (currentNumber > 0) {
                for (let missing = currentNumber + 1; missing < detectedNumber; missing++) {
                    insertedCount += 1
                    normalized.push({
                        title: `Chương ${missing} (Thiếu)`,
                        content: `[THIEU CHUONG ${missing}]\n\nNoi dung chuong nay dang thieu tu EPUB goc. Vui long bo sung sau.`,
                        detectedChapterNumber: missing,
                        finalNumber: missing,
                        volumeNumber: null,
                        volumeTitle: null,
                        volumeChapterNumber: null,
                        isPlaceholder: true,
                    })
                }
            }

            detectedNumberAssignments += 1
            currentNumber = detectedNumber
            normalized.push({
                ...chapter,
                finalNumber: currentNumber,
                volumeChapterNumber: chapter.volumeChapterNumber,
            })
            continue
        }

        currentNumber += 1
        normalized.push({
            ...chapter,
            finalNumber: currentNumber,
            volumeChapterNumber: chapter.volumeChapterNumber,
        })
    }

    return {
        chapters: normalized,
        insertedCount,
        detectedMax: maxDetected,
        detectedNumberAssignments,
    }
}

async function extractCoverFromEpub(epub: any): Promise<EpubCoverAsset> {
    const manifest = epub.manifest || {}
    const metadataCover = epub.metadata?.cover ? String(epub.metadata.cover) : null

    const candidateIds: string[] = []
    if (metadataCover) candidateIds.push(metadataCover)

    for (const [key, value] of Object.entries(manifest)) {
        const item = value as any
        const id = String(item?.id || key)
        const href = String(item?.href || "")
        const mediaType = String(item?.mediaType || item?.["media-type"] || "")
        const properties = String(item?.properties || "")

        if (
            /cover-image/i.test(properties) ||
            /cover/i.test(id) ||
            /cover/i.test(href)
        ) {
            candidateIds.push(id)
            continue
        }

        if (/image\//i.test(mediaType) && /cover/i.test(href)) {
            candidateIds.push(id)
        }
    }

    const uniqueCandidateIds = Array.from(new Set(candidateIds.filter(Boolean)))
    if (uniqueCandidateIds.length === 0) {
        return { buffer: null, mimeType: null, sourceId: null }
    }

    for (const id of uniqueCandidateIds) {
        const fromImage = await new Promise<EpubCoverAsset>((resolve) => {
            if (typeof epub.getImage !== "function") {
                resolve({ buffer: null, mimeType: null, sourceId: null })
                return
            }

            epub.getImage(id, (err: any, data: any, mimeType?: string) => {
                if (err || !data) {
                    resolve({ buffer: null, mimeType: null, sourceId: null })
                    return
                }

                const buffer = Buffer.isBuffer(data) ? data : Buffer.from(data)
                resolve({ buffer, mimeType: typeof mimeType === "string" ? mimeType : null, sourceId: id })
            })
        })

        if (fromImage.buffer) {
            return fromImage
        }

        const fromFile = await new Promise<EpubCoverAsset>((resolve) => {
            if (typeof epub.getFile !== "function") {
                resolve({ buffer: null, mimeType: null, sourceId: null })
                return
            }

            epub.getFile(id, (err: any, data: any, mimeType?: string) => {
                if (err || !data) {
                    resolve({ buffer: null, mimeType: null, sourceId: null })
                    return
                }

                const buffer = Buffer.isBuffer(data) ? data : Buffer.from(data)
                resolve({ buffer, mimeType: typeof mimeType === "string" ? mimeType : null, sourceId: id })
            })
        })

        if (fromFile.buffer) {
            return fromFile
        }
    }

    return { buffer: null, mimeType: null, sourceId: null }
}

async function saveCoverBufferToR2(cover: EpubCoverAsset): Promise<string | null> {
    if (!cover.buffer) return null

    return uploadBufferToR2({
        buffer: cover.buffer,
        contentType: cover.mimeType,
        keyPrefix: "covers/epub",
        fileNameHint: cover.sourceId || undefined,
    })
}

async function parseEpubSections(tempFilePath: string): Promise<{ metadata: any; sections: EpubSection[]; cover: EpubCoverAsset }> {
    return new Promise((resolve, reject) => {
        const EPub = getPatchedEpubCtor()
        const epub = new EPub(tempFilePath, "", "")

        epub.on("error", (err: any) => reject(err))
        epub.on("end", async () => {
            try {
                const metadata = epub.metadata
                const flow = epub.flow
                const sections: EpubSection[] = []
                const cover = await extractCoverFromEpub(epub)

                for (let i = 0; i < flow.length; i++) {
                    const item = flow[i]
                    const text = await new Promise<string>((res) => {
                        epub.getChapter(item.id, (err: any, data: string) => {
                            if (err) res("")
                            else res(data)
                        })
                    })

                    if (!text || text.trim().length === 0) continue

                    const plainText = convert(text, { wordwrap: false }).trim()
                    if (!plainText) continue

                    sections.push({
                        sourceTitle: item.title || `Mục ${i + 1}`,
                        content: plainText,
                    })
                }

                resolve({ metadata, sections, cover })
            } catch (err) {
                reject(err)
            }
        })

        epub.parse()
    })
}

export async function POST(req: Request) {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        const formData = await req.formData()
        const epubFile = formData.get("file") as File
        const previewOnly = String(formData.get("preview") || "").toLowerCase() === "true"
        const splitMode = normalizeSplitMode(formData.get("splitMode"))
        const seriesMode = normalizeSeriesMode(formData.get("seriesMode"))
        const seriesIdInput = readFormText(formData, "seriesId")
        const seriesNameInput = readFormText(formData, "seriesName")
        const replaceExisting = String(formData.get("replaceExisting") || "").toLowerCase() === "true"
        const appendTargetNovelId = String(formData.get("appendTargetNovelId") || "").trim()

        if (!epubFile) {
            return NextResponse.json({ error: "Thiếu file EPUB" }, { status: 400 })
        }

        const buffer = Buffer.from(await epubFile.arrayBuffer())
        const tempFilePath = path.join(os.tmpdir(), `upload-${Date.now()}.epub`)
        await fs.writeFile(tempFilePath, buffer)

        let parsedData: any = null
        try {
            const { metadata, sections, cover } = await parseEpubSections(tempFilePath)

            let regexNormalized: string | null = null
            let regexPreset: string | null = null
            let chapters: ParsedChapter[] = []

            if (splitMode === "regex") {
                const regexResolved = resolveRegexPattern(formData)
                const compiled = buildRegexFromInput(regexResolved.regexInput)
                chapters = buildChaptersFromRegexSections(sections, compiled.regex)
                regexNormalized = compiled.normalized
                regexPreset = regexResolved.regexPreset

                if (chapters.length === 0) {
                    return NextResponse.json(
                        {
                            error: "Regex không tách được chương nào. Hãy thử regex khác hoặc chuyển về TOC.",
                            parserInfo: {
                                splitMode,
                                chapterRegexUsed: regexNormalized,
                                regexPreset,
                                sourceSections: sections.length,
                                chaptersDetected: 0,
                            }
                        },
                        { status: 400 }
                    )
                }
            } else {
                chapters = buildChaptersFromTOCSections(sections)
                if (chapters.length === 0) {
                    return NextResponse.json(
                        { error: "Không tìm thấy chương hợp lệ từ TOC. Bạn có thể thử chế độ Regex." },
                        { status: 400 }
                    )
                }
            }

            const leadingTrimmed = trimLeadingBeforeChapterOne(chapters)
            const gapFilled = withMissingChapterPlaceholders(leadingTrimmed.chapters)

            parsedData = {
                metadata,
                sections,
                chapters: gapFilled.chapters,
                cover,
                parserInfo: {
                    splitMode,
                    chapterRegexUsed: regexNormalized,
                    regexPreset,
                    sourceSections: sections.length,
                    chaptersDetected: chapters.length,
                    trimmedBeforeChapterOne: leadingTrimmed.trimmedCount,
                    chaptersFinal: gapFilled.chapters.length,
                    insertedMissingChapters: gapFilled.insertedCount,
                    detectedMaxChapterNumber: gapFilled.detectedMax,
                    detectedNumberAssignments: gapFilled.detectedNumberAssignments,
                }
            }
        } finally {
            // Xóa file tạm
            await fs.unlink(tempFilePath).catch(() => { })
        }

        const { metadata, chapters, parserInfo, cover } = parsedData

        const metadataTitle = normalizeMetaText(metadata?.title, "Truyện chưa đặt tên")
        const metadataAuthor = normalizeMetaText(metadata?.creator, "Khuyết danh")
        const metadataDescRaw = normalizeMetaText(metadata?.description, "Chưa có giới thiệu")
        const metadataDesc = convert(metadataDescRaw, { wordwrap: false })
        const detectedGenreNames = extractGenreCandidatesFromMetadata(metadata)

        const novelTitle = normalizeMetaText(readFormText(formData, "title"), metadataTitle)
        const novelAuthor = normalizeMetaText(readFormText(formData, "authorName"), metadataAuthor)
        const novelDesc = normalizeMetaText(readFormText(formData, "description"), metadataDesc)
        const importDefaultStatus = "Hoàn thành"

        const hasDetectedVolumes = chapters.some((ch: any) => ch.volumeNumber !== null)

        if (previewOnly) {
            return NextResponse.json({
                preview: true,
                fileName: epubFile.name,
                splitMode,
                detectedStructureType: hasDetectedVolumes ? "light_novel" : "standard",
                parserInfo,
                hasCoverFromEpub: !!cover?.buffer,
                novel: {
                    title: novelTitle,
                    authorName: novelAuthor,
                    description: novelDesc,
                    detectedGenres: detectedGenreNames,
                    totalChapters: chapters.length,
                },
                chaptersPreview: chapters.slice(0, 20).map((ch: any, i: number) => ({
                    number: ch.finalNumber || i + 1,
                    title: ch.title,
                    isPlaceholder: !!ch.isPlaceholder,
                    volumeNumber: ch.volumeNumber,
                    volumeTitle: ch.volumeTitle,
                    volumeChapterNumber: ch.volumeChapterNumber,
                    excerpt: (ch.content || "").slice(0, 180),
                })),
            })
        }

        let targetNovelId = ""
        let responseStatus = 201
        let replaced = false
        let isAppending = !!appendTargetNovelId
        let finalCoverUrl: string | null = null

        if (isAppending) {
            const targetNovel = await prisma.novel.findUnique({ where: { id: appendTargetNovelId } })
            if (!targetNovel || !canReplaceNovelByRole(session.user.role as UserRole, session.user.id, targetNovel)) {
                return NextResponse.json({ error: "Truyện không tồn tại hoặc không đủ quyền thao tác báo cáo bổ sung." }, { status: 403 })
            }
            targetNovelId = targetNovel.id
            responseStatus = 200
            finalCoverUrl = targetNovel.coverUrl
        } else {
            const duplicatedNovel = await findNovelByTitleInsensitive(novelTitle)
            const canReplaceDuplicated = duplicatedNovel ? canReplaceNovelByRole(session.user.role as UserRole, session.user.id, duplicatedNovel) : false

            if (duplicatedNovel && !replaceExisting) {
                return NextResponse.json({
                    code: "DUPLICATE_TITLE",
                    error: `Truyện "${duplicatedNovel.title}" đã tồn tại`,
                    canReplace: canReplaceDuplicated,
                    existingNovel: { id: duplicatedNovel.id, title: duplicatedNovel.title, slug: duplicatedNovel.slug },
                }, { status: 409 })
            }

            if (duplicatedNovel && replaceExisting && !canReplaceDuplicated) {
                return NextResponse.json({
                    code: "DUPLICATE_TITLE",
                    error: "Bạn không có quyền replace truyện đã tồn tại",
                    canReplace: false,
                    existingNovel: { id: duplicatedNovel.id, title: duplicatedNovel.title, slug: duplicatedNovel.slug },
                }, { status: 403 })
            }

            const resolvedGenreIds = await resolveGenreIdsFromNames(detectedGenreNames, true)
            const selectedSeriesId = await resolveSeriesIdForEpubImport({
                mode: seriesMode,
                seriesId: seriesIdInput,
                seriesName: seriesNameInput,
                userRole: session.user.role,
                userId: session.user.id,
            })

            const coverUrl = await saveCoverBufferToR2(cover)
            finalCoverUrl = coverUrl
            targetNovelId = duplicatedNovel?.id || ""

            if (duplicatedNovel && replaceExisting) {
                const updatedNovel = await prisma.$transaction(async (tx) => {
                    await tx.novel.update({
                        where: { id: duplicatedNovel.id },
                        data: {
                            title: novelTitle,
                            authorName: novelAuthor,
                            description: novelDesc,
                            status: importDefaultStatus,
                            coverUrl,
                            seriesId: selectedSeriesId,
                            totalChapters: chapters.length,
                            ...(session.user.role === "MOD" ? { uploaderId: session.user.id } : {}),
                        },
                    })
                    await tx.novelGenre.deleteMany({ where: { novelId: duplicatedNovel.id } })
                    if (resolvedGenreIds.length > 0) {
                        await tx.novelGenre.createMany({
                            data: resolvedGenreIds.map((genreId) => ({ novelId: duplicatedNovel.id, genreId })),
                            skipDuplicates: true,
                        })
                    }
                    return tx.novel.findUnique({ where: { id: duplicatedNovel.id } })
                })

                if (!updatedNovel) throw new Error("Không thể replace truyện đã tồn tại")

                targetNovelId = updatedNovel.id
                responseStatus = 200
                replaced = true

                if (duplicatedNovel.coverUrl && duplicatedNovel.coverUrl !== coverUrl) {
                    await deleteR2ObjectByUrl(duplicatedNovel.coverUrl).catch(() => { })
                }
            } else {
                const baseSlug = slugify(novelTitle)
                let slug = baseSlug
                let slugCounter = 1
                while (await prisma.novel.findUnique({ where: { slug } })) {
                    slug = `${baseSlug}-${slugCounter}`
                    slugCounter++
                }

                const createData: any = {
                    title: novelTitle,
                    slug,
                    authorName: novelAuthor,
                    description: novelDesc,
                    status: importDefaultStatus,
                    coverUrl,
                    seriesId: selectedSeriesId,
                    uploaderId: session.user.id,
                    totalChapters: chapters.length,
                }
                if (resolvedGenreIds.length > 0) {
                    createData.genres = { create: resolvedGenreIds.map((genreId) => ({ genre: { connect: { id: genreId } } })) }
                }

                const createdNovel = await prisma.novel.create({ data: createData })
                targetNovelId = createdNovel.id
            }
        }

        await connectToMongoDB()
        
        let insertedCount = 0
        let updatedCount = 0

        if (!isAppending) {
            await Chapter.deleteMany({ novelId: targetNovelId })
            const finalChapterDocs = chapters.map((ch: any, i: number) => ({
                novelId: targetNovelId,
                number: ch.finalNumber || (i + 1),
                volumeNumber: ch.volumeNumber ?? null,
                volumeTitle: ch.volumeTitle ?? null,
                volumeChapterNumber: ch.volumeChapterNumber ?? null,
                title: ch.title,
                content: ch.content,
                views: 0,
            }))
            if (finalChapterDocs.length > 0) {
                await Chapter.insertMany(finalChapterDocs)
                insertedCount = finalChapterDocs.length
            }
        } else {
            const bulkOps = chapters.map((ch: any) => {
                const candidateNumber = ch.detectedChapterNumber || ch.finalNumber
                return {
                    updateOne: {
                        filter: { novelId: targetNovelId, number: candidateNumber },
                        update: {
                            $set: {
                                volumeNumber: ch.volumeNumber ?? null,
                                volumeTitle: ch.volumeTitle ?? null,
                                volumeChapterNumber: ch.volumeChapterNumber ?? null,
                                title: ch.title,
                                content: ch.content,
                            },
                        },
                        upsert: true,
                    }
                }
            })
            
            if (bulkOps.length > 0) {
                const writeResult = await Chapter.bulkWrite(bulkOps)
                insertedCount = writeResult.upsertedCount || 0
                updatedCount = writeResult.modifiedCount || 0
            }

            const totalAfterMerge = await Chapter.countDocuments({ novelId: targetNovelId })
            await prisma.novel.update({
                where: { id: targetNovelId },
                data: { totalChapters: totalAfterMerge },
            })
        }

        const novelAfterWrite = await prisma.novel.findUnique({ where: { id: targetNovelId } })
        if (!novelAfterWrite) {
            throw new Error("Không thể tải lại thông tin truyện sau khi import")
        }

        return NextResponse.json({
            ...novelAfterWrite,
            parserInfo,
            hasCoverFromEpub: !!finalCoverUrl,
            detectedGenres: detectedGenreNames,
            replaced,
        }, { status: responseStatus })
    } catch (error: any) {
        if (isRequestAbortedError(error)) {
            console.warn("EPUB upload aborted by client or network interruption")
            return NextResponse.json({ error: "Kết nối upload bị ngắt trong lúc xử lý" }, { status: 499 })
        }

        console.error("EPUB upload error:", error)
        return NextResponse.json({ error: "Lỗi xử lý file EPUB", details: error.message }, { status: 500 })
    }
}
