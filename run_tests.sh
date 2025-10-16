
#!/bin/bash
# Simple test runner for AI-Briefing platform

echo "Running AI-Briefing tests..."
echo "============================"

# Run tests with coverage
python3 -m pytest tests/ -v --cov=. --cov-report=term-missing

# Check exit code
if [ $? -eq 0 ]; then
    echo ""
    echo "✅ All tests passed!"
else
    echo ""
    echo "❌ Some tests failed. Please check the output above."
    exit 1
fi
