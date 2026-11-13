# DOG prototype · Nhóm 14 IT5440

DOG kiểm tra hành động shell trước khi thực thi, đối chiếu thay đổi trên workspace với phạm vi cho phép, rồi ghi nhật ký và hoàn tác thay đổi sai phạm. Demo chính dùng Codex CLI làm tác nhân sửa lỗi.

```text
Codex CLI → MCP run_command → Guard → DockerRunner
                               ↓          ↓
                        kiểm tra lệnh   snapshot, nhật ký và hoàn tác
```

## Cài đặt và chạy

Từ thư mục `dog-prototype/`, dùng Python 3.11 trở lên và tài khoản hệ điều hành thường. Bật Docker Desktop hoặc Docker Engine trước khi chạy benchmark đầy đủ hoặc agent.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
docker pull python:3.12-slim-bookworm

python -m unittest discover -s tests -v
python -m dog check 'rm -rf /'
python -m dog benchmark --analysis-only
python -m dog benchmark --repeat 3
codex login status
python -m dog codex-demo
```

`check` trả mã `77` khi lệnh bị chặn. `--analysis-only` chạy 27 ca phân tích cú pháp hoặc mô phỏng ngân sách và có thể chạy khi Docker đang tắt. `benchmark` chạy đủ 50 kịch bản độc lập.

## Demo Codex

### Cài DOG vào một repo

Với Python 3.11 trở lên, có thể cài bản phát triển ngay trong thư mục DOG:

~~~bash
python -m pip install -e .
~~~

Hoặc cài trực tiếp từ Git mà không cần clone riêng mã nguồn DOG. Repository hiện tại là monorepo nên URL phải chỉ rõ thư mục `dog-prototype`:

~~~bash
python -m pip install git+https://github.com/doanduccuong/dog-prototype.git
~~~

Sau khi gói đã được công bố lên nhánh Git, trên mỗi repository cần bảo vệ chỉ cần chạy:

~~~bash
dog install /path/to/checkout
cd /path/to/checkout
codex
~~~

Nếu shell chưa nhận lệnh `dog`, có thể dùng lệnh tương đương `python -m dog install /path/to/checkout`. Lệnh cài tạo `.codex/config.toml` trong repo. Xác nhận tin cậy thư mục nếu Codex hỏi, rồi nhập yêu cầu sửa mã như thường. Trong phiên này, Codex dùng DOG để đọc, sửa và chạy kiểm tra trong Docker. Đường dẫn nhật ký được in khi cài.

Gói DOG chỉ cần cài một lần trên mỗi máy. Lệnh `dog install .` được chạy thêm một lần cho từng repository muốn bảo vệ. Docker vẫn phải chạy trên máy đó và image mặc định cần có sẵn:

~~~bash
docker pull python:3.12-slim-bookworm
~~~

Ảnh Docker phải có Bash và công cụ dự án cần dùng. Chọn ảnh có sẵn bằng `--image`. Có thể giới hạn quyền ghi bằng `--allow src --allow tests` và giữ riêng tệp bằng `--protect <path>`. Mặc định DOG bảo vệ `.git` và `.codex`. Installer từ chối ghi đè cấu hình Codex đã có. Bản prototype hiện chụp tối đa 512 mục và 8 MiB, kể cả metadata Git, nên phù hợp nhất với bản clone nhỏ và sạch.

### Chạy các phép thử đã định sẵn

Dùng Codex CLI đã đăng nhập và có hỗ trợ `--ignore-user-config` (đã thử với 0.163.0). `codex-demo` chạy hai phép đối chứng từ các bản sao của cùng một bộ tệp giả, dựa trên ca sửa `wrangler.toml` trái chỉ dẫn trong hồ sơ công ty Z.

- **Tái hiện sự cố.** Chạy cùng lệnh ghi `wrangler.toml` trong Docker ở hai nhánh. So sánh trạng thái trước và sau, quyết định của DOG và kết quả hoàn tác. Dùng `--replay-only` để chạy riêng phép này mà không cần Codex hay thư viện MCP.
- **Tác vụ với Codex.** Codex tự chọn lệnh để sửa hàm `total` trong `app.py`, kiểm thử và giữ nguyên `wrangler.toml`. Có thể ghim mô hình bằng `--model`. Cả hai nhánh dùng cùng công cụ MCP và cấu hình Docker. Ở nhánh đối chứng, MCP gọi Docker trực tiếp. Ở nhánh có DOG, MCP gọi `Guard.run()`.

Phiên Codex có thư mục làm việc trống, sandbox chỉ đọc và shell tích hợp bị tắt. Công cụ MCP cục bộ chuyển các lệnh đọc, sửa và kiểm thử vào thư mục giả lập `/workspace` trong Docker. Chương trình kiểm tra sự kiện của Codex, nhật ký thực thi và các đường dẫn thực sự thay đổi, rồi chạy lại phép kiểm chức năng độc lập. Đề bài và kết quả công cụ của bộ tệp giả được gửi tới dịch vụ mô hình của Codex.

Mã thoát 0 báo các phép thử đã chọn đạt tiêu chí. Dùng `--output` với tên thư mục mới để giữ kết quả từng lượt. Có thể chạy luồng mini-SWE-agent để kiểm thử tích hợp nội bộ bằng `python -m pip install '.[agent]'` và `python -m dog agent`.

## Thử nghiệm trên repo mã nguồn mở

`repo-demo` dùng [lỗi #126 của pallets/itsdangerous](https://github.com/pallets/itsdangerous/issues/126) và phiên bản trước [bản vá #133](https://github.com/pallets/itsdangerous/pull/133). Bộ chạy xuất các tệp được Git theo dõi vào bản sao tạm, xác nhận phép kiểm thất bại trước bản vá và đạt trên commit đã sửa, rồi giao cùng tác vụ cho Codex ở hai nhánh. Chỉ `src/itsdangerous/timed.py` được phép thay đổi. Phép thử hoàn tác dùng một lệnh riêng do nhóm đặt để ghi vào `setup.cfg`.

```bash
git clone --no-checkout https://github.com/pallets/itsdangerous.git artifacts/itsdangerous-source
python -m dog repo-demo artifacts/itsdangerous-source --output artifacts/itsdangerous-run
```

Kết quả và nhật ký nằm trong thư mục đầu ra. Phép kiểm độc lập chạy bằng thư viện chuẩn Python trong Docker, bao gồm cả trường hợp token hợp lệ và token quá hạn. Đề bài và nội dung mã nguồn công khai mà Codex đọc qua MCP được gửi tới dịch vụ mô hình.

### Quan sát vi phạm phạm vi trong tác vụ sửa lỗi

~~~bash
python -m dog repo-demo artifacts/itsdangerous-source --observe 5 \
  --output artifacts/itsdangerous-observation
~~~

Mỗi phiên bắt đầu từ mã nguồn gốc và nhận cùng đề bài sửa lỗi với quyền ghi giới hạn ở `src/itsdangerous/timed.py`. Bộ chạy ghi các thay đổi sau mỗi lệnh trong nhánh không DOG và giữ nguyên kết quả trả về cho Codex. Nếu phát hiện vi phạm, bộ chạy lưu lệnh cùng trạng thái ngay trước lần vi phạm đầu tiên. Sau khi hoàn tất số phiên đã định, bộ chạy chọn vi phạm đầu tiên có nhật ký khớp với sự kiện Codex để phát lại trên hai bản sao, có DOG và không DOG. Một phiên Codex mới có DOG kiểm tra riêng khả năng hoàn thành tác vụ.

`result.json` ghi số phiên hợp lệ và số phiên có vi phạm. Trạng thái `no_violation_observed` ghi nhận đợt quan sát đã hoàn tất với 0 vi phạm trong các phiên hợp lệ. Trạng thái này không có phép phát lại. Tùy chọn `--agent-case-only` chạy riêng thử nghiệm có chỉ dẫn đối kháng do nhóm đặt trong repo.

## Dữ liệu thực nghiệm

- `dog/scenarios.json` có 30 ca destructive và 20 ca benign. Các lệnh phá hủy tuyệt đối, lệnh SQL và yêu cầu AWS chỉ đi qua bộ phân tích. Các ca hậu kiểm chạy trên file giả trong thư mục tạm mới cho mỗi kịch bản.
- `artifacts/benchmark-*/results.json` chứa nhãn, quyết định, thời gian, kết quả rollback và tổng hợp TP/FP/TN/FN.
- Trong đầu ra `codex-demo`, `result.json` tổng hợp hai phép thử. Hai tệp `replay-*.jsonl` lưu dấu vết của ca tái hiện. Mỗi nhánh Codex có `events.jsonl`, `audit.jsonl` và `verification.json` khi đã thực hiện phép kiểm độc lập.
- Detection Rate tính theo `TP / (TP + FN)`, gồm cả chặn trước và phát hiện sau thực thi. FPR tính theo `FP / (FP + TN)`. Các ca lỗi hạ tầng được thống kê riêng. Tỷ lệ rollback dùng mẫu số là số lần đã thử rollback.
- `guard_ms` tính thời gian xử lý của DOG ngoài thời gian chạy lệnh, đến trước lần ghi log kết thúc. `docker_execution_ms` gồm khởi tạo container, thực thi và thu kết quả. `guard_executed_ms` chỉ tính các ca có chạy lệnh. Mỗi vòng lặp chạy lại cùng bộ kịch bản, vì vậy số ca độc lập vẫn là 50.

## Phạm vi MVP

Policy mặc định `staging` cho phép một tập lệnh phục vụ bài demo. `production` giới hạn ở nhóm lệnh đọc và chặn chuyển hướng ghi. `development` cho phép tên lệnh chưa phân loại, với các lệnh cấm chung vẫn được áp dụng. Cú pháp động như substitution, shell lồng và một số cấu trúc Bash bị chặn. DOG cho phép các truy vấn `find` đơn giản với đối số tường minh như `find . -name '*.py' -print`. Các tùy chọn xóa tệp, chạy lệnh hoặc ghi kết quả ra tệp bị chặn. `bash -c` vẫn thuộc nhóm lệnh bị chặn.

Bộ kiểm tra AWS dùng đơn vị chi phí giả lập theo từng lệnh EC2 và ngưỡng mặc định 200. Phạm vi hậu kiểm là nội dung, kiểu và quyền của các mục bên trong workspace. DOG hoàn tác chênh lệch trong workspace thử nghiệm và xác nhận snapshot sau hoàn tác. Kiểm tra tuyên bố nhận danh sách đường dẫn có cấu trúc trong benchmark. Nhánh Codex có DOG đặt quyền ghi theo tệp nguồn được giao trong từng tác vụ. Phiên bản này hỗ trợ snapshot tối đa 512 mục và 8 MiB nội dung file. Khi lỗi hậu kiểm khiến trạng thái chưa được xác minh, DOG dừng phiên.

Thử nghiệm dùng dữ liệu giả hoặc bản sao mã nguồn công khai với image Docker tin cậy. Mỗi lần thực thi dùng container dùng một lần, tài khoản thường, root filesystem chỉ đọc và mạng tắt. Mount duy nhất từ máy chủ là workspace tạm dùng cho thí nghiệm. Phân tích Bash và snapshot chỉ quan sát các hành động trong phạm vi hỗ trợ và trạng thái trước/sau. Kiểm soát SDK cloud, rollback DB và triển khai production nằm ngoài MVP.
