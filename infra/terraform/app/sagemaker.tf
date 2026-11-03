resource "aws_ecr_repository" "sagemaker_unified" {
  count                = var.enable_sagemaker_unified ? 1 : 0
  name                 = "texas-energy-sagemaker-unified"
  image_tag_mutability = "IMMUTABLE"
  encryption_configuration {
    encryption_type = "AES256"
  }
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "sagemaker_unified" {
  count      = var.enable_sagemaker_unified ? 1 : 0
  repository = aws_ecr_repository.sagemaker_unified[0].name
  policy     = jsonencode({ rules = [{ rulePriority = 1, description = "Keep immutable SageMaker runtime releases.", selection = { tagStatus = "untagged", countType = "imageCountMoreThan", countNumber = 1 }, action = { type = "expire" } }] })
}
