# 언제나갈까?

내 위치 → 정류장·버스 선택 → 버스 도착시간과 도보시간으로 "언제 나가면 딱 탈 수 있는지" 알려주는 웹앱 (FastAPI + 네이버 지도).

## 폴더 구성
```
main.py            FastAPI 백엔드 (버스·지하철·도보 API)
static/index.html  메인 화면 (네이버 지도)
static/naver_test.html  네이버 지도 인증 진단용 페이지
requirements.txt   패키지 목록
render.yaml        Render 배포 설정
.gitignore         GitHub에 올리지 않을 파일 목록
```

## 환경변수 (Render의 Environment 탭에 입력)
| 이름 | 설명 | 없으면 |
|---|---|---|
| `GBIS_KEY` | 공공데이터포털 인증키 (경기도·서울시 공통) | 샘플 데이터로 동작 |
| `SEOUL_KEY` | 서울시용 키를 따로 쓸 때 (선택) | `GBIS_KEY` 사용 |
| `TMAP_KEY` | TMAP appKey (도보 경로) | 직선거리 추정 |
| `SEOUL_SUBWAY_KEY` | 서울 열린데이터광장 실시간 지하철 키 | 지하철 기능 꺼짐 |
| `NAVER_CLIENT_ID` | 네이버 지도 Client ID | 코드의 기본값 사용 |

> 지하철 기능은 `SEOUL_SUBWAY_KEY`와 `TMAP_KEY`가 둘 다 있어야 켜집니다.

## Render 배포
1. 이 폴더의 파일을 GitHub 저장소에 올립니다.
2. Render → New → Web Service → 저장소 선택
3. 설정 (`render.yaml`을 쓰는 Blueprint로 만들면 자동 입력됩니다)
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - Region: Singapore, Instance Type: Free
4. Environment에 위 환경변수 입력 → 저장 후 배포
5. 배포된 주소(`https://○○○.onrender.com`)를
   - **네이버 클라우드 콘솔 → Maps 애플리케이션 → Web 서비스 URL**에 추가
   - 서울 열린데이터광장 키의 사용 URL에도 필요하면 추가
6. 확인: `주소/health` 가 `{"ok":true,...}` 로 열리고 키 설정 여부가 `true`로 보이면 정상

## 로컬 실행 (Windows PowerShell)
```powershell
pip install -r requirements.txt
$env:GBIS_KEY="..."; $env:TMAP_KEY="..."; $env:SEOUL_SUBWAY_KEY="..."
python -m uvicorn main:app --reload
```
브라우저에서 http://localhost:8000 (127.0.0.1은 네이버 지도 인증이 안 됩니다)

## 알아둘 점
- Render 무료 플랜은 한동안 요청이 없으면 서버가 잠들어 첫 응답이 느립니다.
- 서버 주소를 아는 사람은 누구나 API를 호출할 수 있어 공공데이터 일일 호출 한도가 소진될 수 있습니다. 주소를 공개적으로 퍼뜨리지 마세요.
- 일부 국내 공공 API는 해외 IP를 막을 수 있습니다. 배포 후 정류장·도착시간 조회가 되는지 먼저 확인하세요.
