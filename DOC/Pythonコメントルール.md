# Pythonコメントルール

このドキュメントでは、本プロジェクトでのPythonコメントの書き方を定義します。
基本方針は、Pythonの標準的な慣習であるPEP 8とdocstringの考え方に従います。

## 基本方針

- 関数やクラスの説明には、通常のコメントではなくdocstringを使用する。
- 処理中の補足には、`#` コメントを使用する。
- コメントは「何をしているか」よりも「なぜ必要か」「どのような前提があるか」を書く。
- コードを読めば分かる内容はコメントしない。
- コメントは対象のコードと同じインデント階層にそろえる。
- `#` の後には半角スペースを入れる。
- 古くなったコメントや、実装と一致しないコメントは残さない。

## 関数のコメント

関数の説明にはdocstringを使用します。
関数の目的、引数、戻り値が分かるように記述します。
引数がある場合は`Args`、戻り値がある場合は`Returns`を必ず記述します。

```python
def calculate_tax(price: int, tax_rate: float) -> int:
    """税込価格を計算する。

    Args:
        price: 税抜価格。
        tax_rate: 税率。

    Returns:
        税込価格。
    """
    return int(price * (1 + tax_rate))
```

引数も戻り値もない短い関数で説明が単純な場合は、1行のdocstringでもよいです。

```python
def notify_completed() -> None:
    """完了通知を送信する。"""
    send_notification("completed")
```

## クラスのコメント

クラスの説明にもdocstringを使用します。
クラスの役割、主な属性、責務が分かるように記述します。
初期化引数や属性がある場合は、`Args`または`Attributes`に必ず記述します。

```python
class User:
    """ユーザー情報を表すクラス。

    Args:
        name: ユーザー名。
        age: 年齢。

    Attributes:
        name: ユーザー名。
        age: 年齢。
    """

    def __init__(self, name: str, age: int) -> None:
        self.name = name
        self.age = age
```

## 関数やクラス内の処理コメント

関数やクラス内で処理に補足を入れる場合は、`#` コメントを使用します。
コメントは、補足したい処理の直前に書くのを基本とします。

良い例:

```python
def calculate_discount(price: int, user_rank: str) -> int:
    """ユーザーランクに応じた割引後価格を返す。"""

    # VIPはキャンペーン割引と併用できないため、固定割引を適用する
    if user_rank == "vip":
        return price - 1000

    return int(price * 0.9)
```

避ける例:

```python
def calculate_discount(price: int, user_rank: str) -> int:
    # user_rankがvipか確認する
    if user_rank == "vip":
        # priceから1000を引く
        return price - 1000

    # priceに0.9をかけて返す
    return int(price * 0.9)
```

上記の避ける例は、コードを読めば分かる内容をそのまま説明しているため、コメントとしての価値が低いです。

## インラインコメント

行末にコメントを書くインラインコメントは、必要な場合だけ使用します。
使用する場合は、コードから2つ以上スペースを空けて書きます。

```python
timeout = 30  # 外部APIの制限に合わせる
```

説明が長くなる場合は、行末ではなく直前の行にコメントを書きます。

```python
# 外部APIが30秒を超える接続を切断するため、少し短めに設定する
timeout = 28
```

## 変数のコメント

変数は、まず名前で意味が分かるようにします。
単純な変数には原則としてコメントを書きません。

```python
tax_rate = 0.10
user_count: int = 0
user_names: list[str] = []
```

コメントが必要な場合は、値を選んだ理由や外部仕様との関係を書きます。

```python
# 外部APIの仕様により、小数第2位までで送信する
amount = round(total_amount, 2)
```

## テーブルのコメント

Python自体にテーブルコメントの標準はありません。
データベースのテーブルやカラムの説明は、可能であればDB側またはORM側に記述します。

SQLの例:

```sql
COMMENT ON TABLE users IS 'ユーザー情報を管理するテーブル';
COMMENT ON COLUMN users.email IS 'ログインに使用するメールアドレス';
```

SQLAlchemyの例:

```python
class User(Base):
    """ユーザーテーブル。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, comment="ユーザーID")
    email = Column(String, nullable=False, comment="メールアドレス")
```

## 判断基準

コメントを書くか迷った場合は、次の基準で判断します。

- 関数やクラスの説明であればdocstringを書く。
- 処理の理由、前提、仕様上の制約を補足したい場合はコメントを書く。
- 単純な代入、条件分岐、戻り値の説明だけならコメントを書かない。
- コメントがなくても名前や型ヒントで十分に伝わる場合は、名前や構造を改善する。

## Codexによるソース修正時コメント修正について

Codexでソースコードを修正する場合、コメントやdocstringが必ず自動で修正されるとは限りません。
修正対象の近くにあるコメントが明らかに古くなっている場合は修正されることがありますが、すべてのコメント差異を自動検出する保証はありません。

そのため、通常の修正依頼では、コード修正と同時にコメントやdocstringも確認するよう明示します。

推奨する依頼文:

```text
コード修正時は、修正箇所周辺のコメント・docstringも確認し、
「DOC/Pythonコメントルール.md」のルールに従って、実装内容と差異があれば一緒に修正してください。
```

複数回修正した後に、特定フォルダ内のコメントをまとめて確認したい場合は、対象フォルダを指定して確認を依頼します。

まとめて確認する場合の依頼文:

```text
XXフォルダのPythonファイルを確認し、ソースとコメントに差異があれば
「DOC/Pythonコメントルール.md」のルールに従い修正内容を反映させてください。
```

運用方針:

- 普段の修正では、修正依頼にコメント・docstringの確認を含める。
- 複数回修正した後や不安がある場合は、フォルダを指定してまとめて確認する。
- 原則として、修正直後にコメントも確認するほうが、影響範囲を判断しやすく精度が高い。
