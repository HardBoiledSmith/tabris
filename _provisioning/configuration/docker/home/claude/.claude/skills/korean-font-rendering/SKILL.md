---
name: korean-font-rendering
description: >
  한글이 포함된 글리프 산출물(matplotlib PNG, PDF, 일부 SVG/HTML 렌더 등)을 만들 때 한글이 깨지거나 네모(□)로 나오지 않게 폰트를 지정하는 방법.
  차트·그래프·이미지·PDF에 한글 라벨·범례·제목을 넣어야 할 때 사용한다. (UTF-8 텍스트 .md/.csv/코드는 폰트와 무관하므로 해당 없음.)
---

# 한글 폰트 렌더링

- **UTF-8 텍스트**(`.md`, `.csv`, 코드 등)는 폰트 설정과 무관하다. 이 스킬이 필요 없다.
- **글리프를 그리는 산출물**(matplotlib PNG, PDF, 일부 SVG/HTML 렌더 등)은 라이브러리 기본 폰트만으로 한글이 깨지거나 네모(□)로 나올 수 있다. 폰트가 설치돼 있어도 자동 선택되지 않는 경우가 많다.
- 이 샌드박스 이미지에는 **Noto Sans CJK KR**(여러 굵기, `fonts-noto-cjk-extra`)이 포함돼 있다. 한글 라벨·범례·제목이 필요하면 **코드에서 폰트 패밀리를 명시**한다.

```python
import matplotlib.pyplot as plt
plt.rcParams["font.family"] = "Noto Sans CJK KR"
```

- 패밀리 문자열이 불확실하면 확인한다: `fc-list | grep -i "noto sans cjk kr"`
