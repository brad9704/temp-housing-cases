SHELL := /bin/bash
-include .env
export

.PHONY: test diskcheck backup

test:
	uv run pytest -q

# RAW_DIR 파티션 여유 공간이 MIN_FREE_GB 미만이면 실패
diskcheck:
	@mkdir -p "$(RAW_DIR)"
	@free=$$(df -BG --output=avail "$(RAW_DIR)" | tail -1 | tr -dc '0-9'); \
	echo "RAW_DIR=$(RAW_DIR) free=$${free}GB min=$(MIN_FREE_GB)GB"; \
	if [ "$$free" -lt "$(MIN_FREE_GB)" ]; then echo "여유 공간 부족: 수집 중단"; exit 1; fi

# 원본 HTML 증분 백업 (수집 경로와 무관한 별도 저장소로)
backup:
	@if [ -z "$(BACKUP_DIR)" ]; then \
	  echo "경고: BACKUP_DIR 미설정. 원본 사본이 Spark 디스크 하나뿐입니다."; \
	elif ! command -v rsync >/dev/null; then \
	  echo "rsync가 없습니다: sudo apt install rsync"; exit 1; \
	else \
	  mkdir -p "$(BACKUP_DIR)/raw" && rsync -a --info=stats1 "$(RAW_DIR)/" "$(BACKUP_DIR)/raw/"; \
	fi

# 수집 진행 대시보드 (같은 네트워크에서 http://<서버 IP>:8090)
.PHONY: dashboard
dashboard:
	uv run python -m dashboard.server --host 0.0.0.0 --port 8090
