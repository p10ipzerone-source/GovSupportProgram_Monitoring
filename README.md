# 지원사업 공고 모니터

RIPC 특허 지원사업, 수출바우처, 기업마당(사업화) 공고를 GitHub Actions로 2시간마다(KST 08~20시) 수집해 `data.json`에 저장하고, GitHub Pages의 `index.html`에서 보여준다.

## 설치
1. 새 저장소(Public)에 이 폴더 내용을 그대로 업로드 (`.github/workflows/update.yml` 포함)
2. Settings > Pages > Source: Deploy from a branch, `main` / `(root)`
3. Actions 탭 > "공고 수집" > Run workflow (첫 수집)
4. `https://<아이디>.github.io/<저장소명>/` 접속

## 즉시 갱신 버튼 (선택)
페이지 ⚙ 에서 Fine-grained token 입력 (이 저장소만, Actions: Read and write, Contents: Read).

## 기업마당 키워드 수정
`scraper.py` 상단 `BIZINFO_KEYWORDS` 목록 편집.

## 기업마당 공식 API (선택)
기업마당 API 인증키가 있으면 Settings > Secrets > Actions에 `BIZINFO_KEY`로 등록. 없으면 목록 페이지를 직접 읽는다.
