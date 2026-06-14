"""
ed25519.py - RFC 8032 Ed25519 署名検証（ピュアPython・検証専用）

外部ライブラリ不要。hashlib (SHA-512) のみを使用する。
Discord Interactions の署名検証（x-signature-ed25519 ヘッダ）向けに実装。

参考: https://datatracker.ietf.org/doc/html/rfc8032
"""
import hashlib

# =====================================================================
# 曲線パラメータ（Ed25519 / RFC 8032 Section 5.1）
# =====================================================================

# 素体の法: p = 2^255 - 19
_P = 2**255 - 19

# 群の位数
_Q = 2**252 + 27742317777372353535851937790883648493

# ねじれパラメータ d = -121665/121666 mod p
_D = -121665 * pow(121666, _P - 2, _P) % _P

# sqrt(-1) mod p （x 座標の復元で使用）
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

# 単位元（無限遠点）の拡張座標表現
_IDENTITY = (0, 1, 1, 0)


# =====================================================================
# 有限体演算
# =====================================================================

def _inv(x: int) -> int:
    """フェルマーの小定理による逆元: x^(p-2) mod p"""
    return pow(x, _P - 2, _P)


def _recover_x(y: int, sign: int):
    """
    y 座標と符号ビットから x 座標を復元する。
    失敗（曲線上に点が存在しない）場合は None を返す。
    """
    y2 = y * y % _P
    # x^2 = (y^2 - 1) / (d * y^2 + 1) mod p
    x2 = (y2 - 1) * _inv(_D * y2 + 1) % _P

    if x2 == 0:
        return 0 if sign == 0 else None

    # x = x2^((p+3)/8) mod p （p ≡ 5 mod 8 の場合のアルゴリズム）
    x = pow(x2, (_P + 3) // 8, _P)

    if x * x % _P == x2:
        pass  # 正しい値が得られた
    elif x * x % _P == _P - x2:
        x = x * _SQRT_M1 % _P  # sqrt(-1) を掛けて符号を補正
    else:
        return None  # x^2 = x2 の解が存在しない

    if x * x % _P != x2:
        return None

    # 符号ビットが一致しない場合は x を符号反転
    if x % 2 != sign:
        x = _P - x

    return x


# =====================================================================
# 拡張ねじれエドワーズ座標
# 点を (X, Y, Z, T) で表す。(x, y) = (X/Z, Y/Z)、T = XY/Z
# =====================================================================

def _point_add(P1: tuple, P2: tuple) -> tuple:
    """2点の加算（拡張ねじれエドワーズ座標）"""
    X1, Y1, Z1, T1 = P1
    X2, Y2, Z2, T2 = P2
    A = (Y1 - X1) * (Y2 - X2) % _P
    B = (Y1 + X1) * (Y2 + X2) % _P
    C = T1 * 2 * _D * T2 % _P
    D = Z1 * 2 * Z2 % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _point_mul(s: int, point: tuple) -> tuple:
    """スカラー倍算（ダブルアンドアッド法）"""
    result = _IDENTITY
    addend = point
    while s > 0:
        if s & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        s >>= 1
    return result


def _compress(point: tuple) -> bytes:
    """点を 32 バイト（RFC 8032 の符号付き y エンコーディング）に圧縮"""
    X, Y, Z, _ = point
    zi = _inv(Z)
    x = X * zi % _P
    y = Y * zi % _P
    buf = bytearray(y.to_bytes(32, "little"))
    buf[31] |= (x & 1) << 7  # 最上位ビットに x の最下位ビット（符号）を格納
    return bytes(buf)


def _decompress(b: bytes):
    """
    32 バイトを拡張座標の点に展開する。
    不正なエンコーディングの場合は None を返す。
    """
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little") & ~(1 << 255)
    sign = b[31] >> 7
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


# =====================================================================
# ベースポイント B（RFC 8032 Section 5.1）
# y = 4/5 mod p、x は偶数の方
# =====================================================================
_BY = 4 * _inv(5) % _P
_BX = _recover_x(_BY, 0)
_B = (_BX, _BY, 1, _BX * _BY % _P)


# =====================================================================
# 公開 API
# =====================================================================

def verify(public_key_hex: str, message: bytes, signature_hex: str) -> bool:
    """
    Ed25519 署名を検証する（RFC 8032 Section 5.1.7）。

    Args:
        public_key_hex: 公開鍵（64文字の16進数文字列）
                        Discord Developer Portal → General Information → PUBLIC KEY
        message:        署名対象のメッセージバイト列
                        Discord Interactions では (timestamp + body) を bytes にしたもの
        signature_hex:  署名（128文字の16進数文字列）
                        リクエストヘッダ x-signature-ed25519 の値

    Returns:
        True: 署名が正しい（Discord からの正規リクエスト）
        False: 署名が不正（なりすまし、改ざん、検証失敗）
    """
    # --- 入力の parse ---
    try:
        pk_bytes = bytes.fromhex(public_key_hex)
        sig_bytes = bytes.fromhex(signature_hex)
    except (ValueError, AttributeError):
        return False

    if len(pk_bytes) != 32 or len(sig_bytes) != 64:
        return False

    # --- 公開鍵 A を曲線上の点に展開 ---
    A = _decompress(pk_bytes)
    if A is None:
        return False

    # --- 署名を R と s に分割 ---
    R_bytes = sig_bytes[:32]
    s = int.from_bytes(sig_bytes[32:], "little")

    # s < q でなければならない（署名のマリアビリティ対策）
    if s >= _Q:
        return False

    R = _decompress(R_bytes)
    if R is None:
        return False

    # --- h = SHA-512(R || A || M) mod q ---
    h = int.from_bytes(
        hashlib.sha512(R_bytes + pk_bytes + message).digest(),
        "little"
    ) % _Q

    # --- 検証: s * B == R + h * A ---
    # 両辺を圧縮して比較する
    sB = _point_mul(s, _B)
    hA = _point_mul(h, A)
    RhA = _point_add(R, hA)

    return _compress(sB) == _compress(RhA)
