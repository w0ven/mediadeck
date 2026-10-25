# 紫金贵宾厅音频 — 来源、处理与验收

核实与制作日期：2026-10-26。交付为 12 个音效、1 首背景音乐；音频合计 **1,211,762 bytes**。所有音源均来自下列明确标注 **CC0 1.0** 的页面，没有使用 CC-BY、CC-BY-SA 或仅声称“免费”的素材。

**未进行试听。** 选材依据是来源页面的用途/风格描述、原文件名称，以及波形、音高走向、频谱、静音和响度分析；下面的“柔和”“厚重”等是设计目标和分析依据，不是假称听过的主观结论。

## 1. 许可证核实

| 素材 | 来源页面 URL | 作者 | 核实结果 |
|---|---|---|---|
| Casino Audio 1.1 | https://kenney.nl/assets/casino-audio | Kenney Vleugels（Kenney） | 页面 License 为 `Creative Commons CC0`；包内 `License.txt` 同样写明 `License (Creative Commons Zero, CC0)`。页面标签包括 foley、casino、card、chip。 |
| Interface Sounds 1.0 | https://kenney.nl/assets/interface-sounds | Kenney | 页面明确 CC0；包内 `License.txt` 明确 CC0，允许个人、教育及商业项目使用。页面标签包括 interface、click、button。 |
| Music Jingles | https://kenney.nl/assets/music-jingles | Kenney Vleugels（Kenney） | 页面明确 CC0；包内 `License.txt` 明确 CC0。选用 `Pizzicato jingles`，没有使用包内的 `8-Bit jingles`。 |
| Impact Sounds 1.0 | https://kenney.nl/assets/impact-sounds | Kenney | 页面与包内 `License.txt` 均明确 CC0；仅使用木质轻碰撞音。 |
| Jazz Slower | https://opengameart.org/content/jazz-slower | Alex McCulloch，发布账号 Pro Sensory | 页面 `License(s): CC0`，标签为 `Jazz`、`Instrumental`。作者说明：`just include my name, Alex McCulloch. Attribution appreciated but not required.` 本文保留作者署名。 |

许可证正文：https://creativecommons.org/publicdomain/zero/1.0/

这里依据的是各作品/素材包页面的具体许可，不是网站页脚的通用版权声明，也不是搜索结果摘要。

## 2. 逐文件来源、选材理由与实测结果

所有时长及 LUFS 均从**最终 MP3 重新解码后**测量；大小为精确 bytes。处理栏还应结合下一节的共同处理流程。原文件路径相对于相应素材包根目录。

| 交付文件 | 来源页面、作者、许可证 | 原始文件名 | 选材理由及专属处理 | 时长（秒） | 大小（bytes） | 实测 LUFS |
|---|---|---|---|---:|---:|---:|
| `deal.mp3` | [Casino Audio][casino]；Kenney Vleugels；CC0 | `Audio/card-slide-1.ogg` | 纸牌滑动拟音，保留细微摩擦；叠两次，相隔 105ms，第二次较轻；0.98/1.02 倍速，4.3kHz 低通。 | 0.452109 | 6,269 | -16.02 |
| `flip.mp3` | [Casino Audio][casino]；Kenney Vleugels；CC0 | `Audio/card-place-1.ogg` | 单张牌的轻落点，映射为翻面后的落牌声，不混入多张洗牌；1.05 倍速，4.3kHz 低通。原录音名称是 card-place，并非声称原文件就是 flip。 | 0.421179 | 5,955 | -16.00 |
| `chip.mp3` | [Casino Audio][casino]；Kenney Vleugels；CC0 | `Audio/chip-lay-3.ogg`；`Audio/chips-stack-4.ogg`；`Audio/chip-lay-1.ogg` | 几枚筹码落下，0/55/115ms 轻重错落；4.7kHz 低通削弱原素材较亮的碰撞高频。 | 0.266372 | 4,075 | -15.99 |
| `raise.mp3` | [Casino Audio][casino]；Kenney Vleugels；CC0 | `Audio/chips-stack-3.ogg`；`Audio/chips-stack-4.ogg`；`Audio/chip-lay-3.ogg`；`Audio/chips-stack-1.ogg` | 用堆叠筹码而非单枚，四层相隔 0/80/110/170ms；0.92 倍速降调增厚，5.1kHz 低通。比 chip 更密、更长，而非单纯加大音量。 | 0.406213 | 5,642 | -15.98 |
| `allin.mp3` | [Casino Audio][casino]；Kenney Vleugels；CC0 | `Audio/chips-handle-5.ogg`；`Audio/chips-handle-2.ogg`；`Audio/chips-stack-3.ogg`；`Audio/chips-stack-4.ogg`；`Audio/chip-lay-3.ogg` | 筹码处理声作推移主体，叠加分批滚落和堆叠；五层从 0 延至 700ms，0.85–0.90 倍速，4.8kHz 低通；以更长、更厚的纹理区分全下。 | 0.974195 | 12,538 | -16.00 |
| `fold.mp3` | [Casino Audio][casino]；Kenney Vleugels；CC0 | `Audio/card-shove-4.ogg` | 推牌擦过的连续摩擦，而非硬拍桌面；1.04 倍速，2.8kHz 低通使其退到柔和的中低频。 | 0.778163 | 10,031 | -16.02 |
| `check.mp3` | [Impact Sounds][impact]；Kenney；CC0 | `Audio/impactWood_light_000.ogg` | 木质轻敲拟音，避免把过牌做成电子确认音；两下相隔 135ms，第二下较轻；1.12 倍速，2.8kHz 低通。 | 0.237687 | 3,761 | -16.00 |
| `turn.mp3` | [Music Jingles][jingles]；Kenney Vleugels；CC0 | `Audio/Pizzicato jingles/jingles_PIZZI08.ogg` | 短双音上行拨弦质感，频谱主能量位于较低音区，避免尖锐警报；3.5kHz 低通。 | 0.563787 | 7,523 | -16.00 |
| `tick.mp3` | [Interface Sounds][interface]；Kenney；CC0 | `Audio/tick_004.ogg` | 文件用途直接对应滴答；0.85 倍速降调，3.5kHz 低通，保留约 64ms 的短促提示。追加 9ms 尾部包络，抑制 MP3 解码边缘跳变。 | 0.064490 | 1,567 | -16.03 |
| `win.mp3` | [Music Jingles][jingles]；Kenney Vleugels；CC0 | `Audio/Pizzicato jingles/jingles_PIZZI02.ogg` | 音高峰序列上行的拨弦短句，作为克制的获胜反馈，不用老虎机式大响铃；0.86 倍速，55/105ms 极轻反射，3.8kHz 低通。 | 1.106667 | 14,106 | -16.00 |
| `lose.mp3` | [Music Jingles][jingles]；Kenney Vleugels；CC0 | `Audio/Pizzicato jingles/jingles_PIZZI09.ogg` | 双音下行，短而中性；不使用失败蜂鸣或夸张嘲讽乐句；0.92 倍速，3kHz 低通。 | 0.610794 | 8,150 | -16.00 |
| `tap.mp3` | [Interface Sounds][interface]；Kenney；CC0 | `Audio/drop_002.ogg` | 界面素材中的短促轻触主体，仅取前约 65ms；2.5kHz 低通，避免亮而刺耳的 click。 | 0.064989 | 1,567 | -16.00 |
| `music.mp3` | [Jazz Slower][music]；Alex McCulloch / Pro Sensory；CC0 | `Jazz Take 2.wav` | 来源明确标记 Jazz、Instrumental，标题指向较慢的爵士。选取内部相容乐段，循环交叉淡化；30Hz 高通、5.5kHz 低通；保留立体声，降低至背景响度。具体循环方法见第 4 节。 | 70.588231 | 1,130,578 | -22.00 |

[casino]: https://kenney.nl/assets/casino-audio
[interface]: https://kenney.nl/assets/interface-sounds
[jingles]: https://kenney.nl/assets/music-jingles
[impact]: https://kenney.nl/assets/impact-sounds
[music]: https://opengameart.org/content/jazz-slower

## 3. 技术处理与验收口径

- 工具：Python（NumPy、soundfile / libsndfile 1.2.2、pyloudnorm），MP3 编码器为 LAME 3.100。
- NumPy 实现单声道混合、剪辑、基于短窗 RMS 的首尾静音裁切、轻微变速插值、平滑频率响应滤波、瞬态压缩/限峰、叠加及淡化。源素材均为 44.1kHz，输出保留此采样率；没有降低音乐采样率或改成单声道。
- 音效压缩为 **MPEG-1 Layer III、单声道、44.1kHz、96kbps CBR**。soundfile 参数为 `compression_level=0.75`、`bitrate_mode='CONSTANT'`，且逐帧读取 MP3 头确认实际码率，不以参数值或“大小÷时长”猜测。
- 音效去静音时保留纸牌摩擦和乐句自然尾音，而不是只保留最大瞬态；混合后再次裁切安静尾部。最终使用 4–6ms 淡入、6ms 淡出；tick 另加 9ms 尾部包络。短音效解码后，首尾最外侧采样均低于 -60dBFS，未插入用于凑时长的静音。
- 用 pyloudnorm 的 BS.1770 型积分响度测量、增益调整与峰值控制完成 loudnorm 的等效处理，并反复检查**编码后的**响度。音效实测范围 **-16.032 至 -15.985 LUFS**。
- **短声音测量约定：** BS.1770 常用积分块为 400ms。`chip`、`check`、`tick`、`tap` 不足 400ms，仅在测量副本的末尾补零到 400ms，再使用同一 400ms 门限算法；补零没有写入交付 MP3。这是短事件能量的统一比较口径，不等同于短音效具有 400ms 的实际长度，也不能代替主观响度试听。
- 平滑限峰用于防止为追求 -16 LUFS 而产生削波，尤其处理纸牌/筹码的高峰均比。所有输出均无超出满幅的采样；4×窗化 sinc 插值峰值估计，音效最高约 **-1.16dBFS**，音乐约 **-7.02dBFS**。这是插值估计，未冒称经过专用真峰值仪器认证。
- 频谱检查：部分原始筹码音在 6kHz 以上的能量占比很高；处理后所有音效的该占比不超过约 **0.56%**（全段 Hann 窗功率谱口径）。这支持“削弱刺耳高频”的处理方向，但不能由频谱单独断言主观听感。
- 所有音效均小于 **40,000 bytes**；音乐小于 **1,500,000 bytes**，按比 40KiB/1.5MiB 更严格的十进制上限验收。win 长度为 1.107 秒，符合 1–2.5 秒。

## 4. 背景音乐风格与循环衔接

风格依据为作者的 **Jazz、Instrumental** 标签与 **Jazz Slower** 标题。处理方向是舒缓、温暖、中低频为主的爵士器乐大厅背景，保留原始立体声而不增加耀眼的电子音色。没有试听确认具体配器，因此不把它描述成已听验的钢琴、萨克斯或真实乐队录音；无人声判断依据是作者的 Instrumental 标注。

循环制作：

1. 原 WAV 长约 176.471 秒，末段有明显衰减及静音，未使用该收尾。
2. 在重复节奏结构中选取 **38.823537–109.411769 秒**作为主体，另取后续至 **111.176485 秒**的音频作重叠尾部。两段待重叠音频的波形相关系数约 **0.696**，是选择相容接点的一个依据；没有把自动节拍估计冒充作者给定的 BPM。
3. 在循环开头对尾部延续与主体开头做 **1.764717 秒升余弦交叉淡化**：权重分别为 `1-w` 和 `w`，而非淡到静音。随后将整个循环移位 **1.156599 秒**，把文件边界放在交叉段中的自然低能量点。有效循环长度保持 **70.588231 秒**。
4. 按周期边界滤波，响度调到 **-22.00 LUFS**；约比音效轻 6 LU。编码为 **立体声、44.1kHz、128kbps CBR**，`compression_level=0.65`。未为压缩体积牺牲立体声或采样率。
5. MP3 保留 **Info/LAME gapless 元数据**，编码延迟 576 个样本、尾填充 1491 个样本。重新解码得到 **3,112,941 个有效采样帧**，与循环 PCM 长度相符，没有把编码器填充当作音乐内容。

解码后接缝验证：

- 最后一个采样到第一个采样的最大声道跳变量为 **0.0003621（-68.82dBFS）**。
- 首/尾 20ms RMS 分别为 **-38.44 / -37.64dBFS**，未在接缝处插入静音或整曲淡出。
- 以 10ms 能量块、-60dBFS 阈值检查，未发现静音块；最安静 100ms 窗约 **-46.55dBFS**。
- 立体声相关系数约 **0.384**，并非复制单声道伪装成立体声。

以上验证的是素材及其 gapless 解码结果。Telegram/WebView 实际播放器是否正确使用无缝元数据、是否因重新创建播放器而产生调度空隙，不属于仅凭音频文件能保证的部分；本次未改动前端，也未做 Telegram 实机试听。

## 5. 已知限制与建议听验项目

- **没有人工试听。** 所有主观风格判断均有上述依据，但耳感、重复疲劳、手机扬声器表现仍应由真人实机确认；不能把指标达标当成听感达标的证明。
- `flip.mp3` 采用单张牌放下拟音表现翻面落点，`allin.mp3` 是筹码处理/堆叠的混合设计，而非声称下载到了同名真实动作录音。建议优先听验 flip 与动画动作的匹配；未发现必须更换的许可或技术问题。
- 原爵士 WAV 所选范围中，有 231 个声道样本达到 16-bit 满幅，约占 0.00362%，最长连续 34 个采样（约 0.77ms）。最终音乐已留足峰值余量，但降低电平并不能证明修复了源文件已有的削波失真。若真人听验发现轻微毛刺，优先考虑替换音乐原曲；本次不能确认这些极短满幅段是否可闻。
- 筹码高频经过明显柔化，换取贵宾厅背景下的克制感；如真人听验觉得偏闷，可在同一 CC0 音源基础上稍减高频削减，而不是直接增加总体响度。

## 6. 下载定位与校验

以下 SHA-256 对应实际下载的原始包/文件，便于将来核对；交付不包含未经处理的包或包内脚本。

| 下载对象 | 直接下载 URL | SHA-256 |
|---|---|---|
| `kenney_casino-audio.zip` | https://kenney.nl/media/pages/assets/casino-audio/2472606a04-1721639069/kenney_casino-audio.zip | `f36250766ac5bc378c13708ddf12a23a8e54a3251f8d482c7536e51b5dbafa18` |
| `kenney_interface-sounds.zip` | https://kenney.nl/media/pages/assets/interface-sounds/fa43c1dd4d-1677589452/kenney_interface-sounds.zip | `f2193d072726d6758a5f7871b2dcc54dcce0d5c35c6f0a62f92549b327c81232` |
| `kenney_music-jingles.zip` | https://kenney.nl/media/pages/assets/music-jingles/f37e530b9e-1677590399/kenney_music-jingles.zip | `b729ba57959bd58793d2c5cafa348aaf2655d354f3da35ec4729e03ec77197b8` |
| `kenney_impact-sounds.zip` | https://kenney.nl/media/pages/assets/impact-sounds/87b4ddecda-1677589768/kenney_impact-sounds.zip | `029d734af1582474edf3a694d1b0cebc97c1c152f2f39fa34d4c2bafc5de77f8` |
| `Jazz Take 2.wav` | https://opengameart.org/sites/default/files/Jazz%20Take%202.wav | `da5279d11c799c7e4ebba11de4bac4e99ff0011611d18fffbdb3afb2924ffd3e` |

