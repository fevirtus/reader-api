import mongoose, { Document, Schema } from "mongoose"

export interface IUserRecommendation extends Document {
  userId: string
  novelId: string
  createdAt: Date
  updatedAt: Date
}

const UserRecommendationSchema: Schema = new Schema(
  {
    userId: { type: String, required: true, index: true },
    novelId: { type: String, required: true, index: true },
  },
  {
    timestamps: true,
  }
)

UserRecommendationSchema.index({ userId: 1, novelId: 1 }, { unique: true })
UserRecommendationSchema.index({ createdAt: -1 })

export const UserRecommendation =
  mongoose.models.UserRecommendation ||
  mongoose.model<IUserRecommendation>("UserRecommendation", UserRecommendationSchema)
