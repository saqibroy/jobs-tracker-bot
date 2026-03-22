#!/usr/bin/env bash
# scripts/dev.sh — Local Docker development workflow
#
# Usage:
#   ./scripts/dev.sh build      Build the Docker image
#   ./scripts/dev.sh up         Start container (detached)
#   ./scripts/dev.sh down       Stop container
#   ./scripts/dev.sh restart    Rebuild & restart
#   ./scripts/dev.sh logs       Tail container logs
#   ./scripts/dev.sh test       Run tests inside container
#   ./scripts/dev.sh shell      Open a shell in the running container
#   ./scripts/dev.sh scan       Trigger a one-off scan (dry-run)
#   ./scripts/dev.sh status     Show container status + health
#   ./scripts/dev.sh clean      Stop container, remove image & volumes
#   ./scripts/dev.sh size       Show Docker image size

set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose"

# Use dev override if it exists
if [ -f docker-compose.dev.yml ]; then
    COMPOSE="docker compose -f docker-compose.yml -f docker-compose.dev.yml"
fi

cmd="${1:-help}"

case "$cmd" in
    build)
        echo "🔨 Building Docker image..."
        $COMPOSE build
        echo "✓ Build complete"
        $0 size
        ;;

    up)
        echo "🚀 Starting container..."
        $COMPOSE up -d
        sleep 3
        $0 status
        ;;

    down)
        echo "🛑 Stopping container..."
        $COMPOSE down
        echo "✓ Stopped"
        ;;

    restart)
        echo "🔄 Rebuilding & restarting..."
        $COMPOSE up -d --build
        sleep 5
        $0 status
        ;;

    logs)
        $COMPOSE logs -f --tail=50
        ;;

    test)
        echo "🧪 Running tests..."
        $COMPOSE run --rm --no-deps job-bot python -m pytest tests/ -v --tb=short
        ;;

    shell)
        echo "🐚 Opening shell..."
        $COMPOSE exec job-bot /bin/bash || $COMPOSE exec job-bot /bin/sh
        ;;

    scan)
        echo "🔍 Running dry-run scan..."
        $COMPOSE exec job-bot python main.py --dry-run --verbose
        ;;

    status)
        echo "📊 Container status:"
        $COMPOSE ps
        echo ""
        # Health check
        if curl -sf http://localhost:8080/health 2>/dev/null; then
            echo ""
            echo "✓ Health endpoint OK"
        else
            echo "⚠ Health endpoint not responding (container may still be starting)"
        fi
        ;;

    clean)
        echo "🧹 Cleaning up..."
        $COMPOSE down --rmi local -v
        echo "✓ Cleaned"
        ;;

    size)
        echo "📦 Image size:"
        docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}" | grep -E "job|REPOSITORY" || true
        ;;

    help|*)
        echo "Usage: $0 {build|up|down|restart|logs|test|shell|scan|status|clean|size}"
        echo ""
        echo "Commands:"
        echo "  build     Build the Docker image"
        echo "  up        Start container (detached)"
        echo "  down      Stop container"
        echo "  restart   Rebuild & restart"
        echo "  logs      Tail container logs"
        echo "  test      Run tests inside container"
        echo "  shell     Open a shell in the running container"
        echo "  scan      Trigger a one-off dry-run scan"
        echo "  status    Show container status + health"
        echo "  clean     Stop, remove image & volumes"
        echo "  size      Show Docker image size"
        ;;
esac
