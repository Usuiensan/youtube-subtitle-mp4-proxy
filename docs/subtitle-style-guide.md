# 字幕デザイン変更手順

字幕は ffmpeg/libass の `subtitles` フィルタで焼き込んでいます。見た目は環境変数で変更します。

## 1. 現在の設定ファイル

Oracle VM デプロイ手順どおりなら、設定はここです。

```bash
/etc/youtube-mp4-proxy.env
```

編集:

```bash
sudo nano /etc/youtube-mp4-proxy.env
```

## 2. 基本設定

現在おすすめの初期値:

```bash
SUBTITLE_FONT=Noto Sans CJK JP
SUBTITLE_FONT_SIZE=20
SUBTITLE_MARGIN_V=34
SUBTITLE_MARGIN_L=24
SUBTITLE_MARGIN_R=24
SUBTITLE_PRIMARY_COLOUR=&H00FFFFFF
SUBTITLE_BACK_COLOUR=&H99000000
```

720pでテレビ字幕より大きすぎないサイズにするなら、まず `20` から始めるのがおすすめです。

## 3. フォントを変える

候補:

```bash
SUBTITLE_FONT=Noto Sans CJK JP
SUBTITLE_FONT=BIZ UDGothic
SUBTITLE_FONT=Rounded M+ 1c
```

サーバーに入っているフォント名を確認:

```bash
fc-list :lang=ja family | sort -u | less
```

Noto Sans CJK を入れる:

```bash
sudo apt install -y fonts-noto-cjk
```

BIZ UD系を使う場合は、Ubuntuのパッケージまたは手動インストールでフォントを入れてから `fc-cache` します。

```bash
fc-cache -fv
```

## 4. 文字サイズを変える

```bash
SUBTITLE_FONT_SIZE=20
```

目安:

```text
18: 小さめ。長い1行を折り返したくない場合
20: 推奨。720pで控えめ
22: やや大きめ
24以上: テレビ字幕風だが、長文は折り返しやすい
```

SRT内に改行がない場所で勝手に折り返される場合は、`2` ずつ下げます。

```bash
SUBTITLE_FONT_SIZE=18
```

## 5. 位置を変える

下からの距離:

```bash
SUBTITLE_MARGIN_V=34
```

数値を大きくすると上に移動します。

```text
24: かなり下
34: 推奨
48: 少し上
64: かなり上
```

左右の余白:

```bash
SUBTITLE_MARGIN_L=24
SUBTITLE_MARGIN_R=24
```

長文の折り返しを減らしたい場合は、左右マージンを小さくします。

```bash
SUBTITLE_MARGIN_L=12
SUBTITLE_MARGIN_R=12
```

## 6. 背景色を変える

現在は半透明黒です。

```bash
SUBTITLE_BACK_COLOUR=&H99000000
```

ASS/SSA の色指定は次の形式です。

```text
&HAABBGGRR
```

意味:

```text
AA: 透明度。00が不透明、FFが透明
BB: 青
GG: 緑
RR: 赤
```

例:

```bash
# 濃い半透明黒
SUBTITLE_BACK_COLOUR=&H99000000

# もっと濃い黒
SUBTITLE_BACK_COLOUR=&H66000000

# 薄い黒
SUBTITLE_BACK_COLOUR=&HBB000000
```

## 7. 文字色を変える

白:

```bash
SUBTITLE_PRIMARY_COLOUR=&H00FFFFFF
```

少し黄色:

```bash
SUBTITLE_PRIMARY_COLOUR=&H00CCFFFF
```

## 8. 変更を反映する

設定ファイルを保存したらサービスを再起動します。

```bash
sudo systemctl restart youtube-mp4-proxy
```

確認:

```bash
curl http://127.0.0.1:8000/healthz
```

## 9. キャッシュについて

字幕スタイルを変えると、内部キャッシュキーも変わります。つまり、古い字幕デザインの動画とは別物として作り直されます。

HLSの内部URL例:

```text
/hls/SpQZyPBAtA0_ja_e74f2a6b/segment_00000.ts
```

末尾の `e74f2a6b` のような部分が字幕スタイル由来のIDです。

古いキャッシュを消したい場合:

```bash
sudo rm -rf /var/cache/youtube-mp4/*
```

特定動画だけ消す場合:

```bash
sudo rm -rf /var/cache/youtube-mp4/VIDEOID_ja_*
```

## 10. おすすめプリセット

### 控えめで折り返しにくい

```bash
SUBTITLE_FONT=Noto Sans CJK JP
SUBTITLE_FONT_SIZE=18
SUBTITLE_MARGIN_V=32
SUBTITLE_MARGIN_L=12
SUBTITLE_MARGIN_R=12
SUBTITLE_PRIMARY_COLOUR=&H00FFFFFF
SUBTITLE_BACK_COLOUR=&H99000000
```

### 現在の推奨

```bash
SUBTITLE_FONT=Noto Sans CJK JP
SUBTITLE_FONT_SIZE=20
SUBTITLE_MARGIN_V=34
SUBTITLE_MARGIN_L=24
SUBTITLE_MARGIN_R=24
SUBTITLE_PRIMARY_COLOUR=&H00FFFFFF
SUBTITLE_BACK_COLOUR=&H99000000
```

### テレビ字幕に少し近い大きめ

```bash
SUBTITLE_FONT=BIZ UDGothic
SUBTITLE_FONT_SIZE=22
SUBTITLE_MARGIN_V=38
SUBTITLE_MARGIN_L=24
SUBTITLE_MARGIN_R=24
SUBTITLE_PRIMARY_COLOUR=&H00FFFFFF
SUBTITLE_BACK_COLOUR=&H99000000
```

## 11. 変更後の確認

HLS:

```bash
curl -L http://YOUR_PUBLIC_IP/youtube-hls/SpQZyPBAtA0/ja
```

MP4:

```bash
curl -L -o test.mp4 http://YOUR_PUBLIC_IP/youtube/SpQZyPBAtA0/ja
```

VRChatで見る前に、手元のプレイヤーで字幕サイズを確認すると調整が早いです。
