#!/bin/bash
# Run tests before deployment
# Exit on first error

set -e

cd "$(dirname "$0")"
source venv/bin/activate

echo "🧪 Running unit tests..."
python -m pytest tests/ -v --tb=short

echo ""
echo "✅ All tests passed!"
