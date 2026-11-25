# 브라우저에서 VPN 고르기

주소는 반드시 `socks5://` 로 적는다. 브라우저는 이 방식에서 이름 해석을 프록시(컨테이너)에 맡기므로
내부 DNS 이름이 VPN 안에서 풀린다. (curl 에서는 같은 동작이 `socks5h://` 다.)

## Chrome: VPN마다 프로필을 따로 띄운다

프로필 폴더를 따로 주면 창 3개를 동시에 열어도 쿠키·세션이 섞이지 않는다.

```sh
open -na "Google Chrome" --args --user-data-dir="$HOME/.chrome-vpn-a" --proxy-server="socks5://127.0.0.1:1081"
open -na "Google Chrome" --args --user-data-dir="$HOME/.chrome-vpn-b" --proxy-server="socks5://127.0.0.1:1082"
open -na "Google Chrome" --args --user-data-dir="$HOME/.chrome-vpn-c" --proxy-server="socks5://127.0.0.1:1083"
```

- 이 창에서는 모든 사이트가 그 VPN 으로 나간다. VPN 이 내부 대역만 받는 설정이면 인터넷 사이트는 열리지 않는다(정상).
- 어느 창이 어느 VPN 인지 헷갈리면 프로필마다 테마 색을 다르게 둔다.
- 창 하나에서 VPN 을 바꿔 가며 쓰려면 FoxyProxy 같은 확장을 쓴다(아래).

### Chrome 의 DNS 미리 읽기 (확인하지 못한 항목)

Chrome 은 SOCKS5 를 쓸 때 사이트 이름을 프록시에 맡긴다. 다만 주소창 입력이나 링크를 미리 읽는 기능이
Mac 쪽 DNS 에 내부 이름을 물을 수 있다고 알려져 있다. **이 구성에서 실제로 새는지는 확인하지 못했다.**
프록시(컨테이너)를 거친 요청은 터널 밖 DNS 로 나가지 않는다 — 이것은 확인했다.

내부 이름이 Mac 의 DNS 에 보이면 안 되는 환경이면, 실행 명령에 아래 옵션을 더하는 방법이 알려져 있다
(여기서 동작을 확인하지는 못했다). 넣은 뒤 `chrome://net-export` 나 DNS 로그로 직접 확인한다.

```
--host-resolver-rules="MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"
```

## Firefox / Chrome + FoxyProxy: 창 하나에서 바꿔 쓴다

1. FoxyProxy 확장을 설치한다.
2. 프록시 3개를 추가한다.

   | 이름 | 종류 | 호스트 | 포트 | 옵션 |
   |---|---|---|---|---|
   | vpn-a | SOCKS5 | 127.0.0.1 | 1081 | Proxy DNS 켬 |
   | vpn-b | SOCKS5 | 127.0.0.1 | 1082 | Proxy DNS 켬 |
   | vpn-c | SOCKS5 | 127.0.0.1 | 1083 | Proxy DNS 켬 |

3. 툴바 아이콘에서 쓸 VPN 을 고른다.

대역이 겹치므로 "주소 패턴으로 자동 선택"은 쓰지 않는다. 같은 `10.0.0.10` 이 VPN마다 다른 서버라서
패턴으로는 구분되지 않는다. 직접 고르거나, Firefox 의 컨테이너 탭마다 프록시를 지정한다.

## 확인

```sh
curl --socks5-hostname 127.0.0.1:1081 http://10.0.0.10/      # vpn-a 의 10.0.0.10
curl --socks5-hostname 127.0.0.1:1082 http://10.0.0.10/      # vpn-b 의 10.0.0.10
```
