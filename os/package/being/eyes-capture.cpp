// Presence emits only the strongest face's position as JSON. Deliberate
// snapshot mode gives its caller one RGB frame on stdout and then exits.

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <csetjmp>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>
#include <vector>

#include <jpeglib.h>

#ifdef __linux__
#include <fcntl.h>
#include <linux/videodev2.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>
#endif

extern "C" {
#include "picornt.h"
}

namespace {

constexpr int scan_extent = 256;
constexpr int snapshot_extent = 320;
constexpr unsigned max_capture_width = 1920;
constexpr unsigned max_capture_height = 1080;
constexpr auto scan_interval = std::chrono::milliseconds(200);
constexpr auto frame_timeout = std::chrono::seconds(3);
using Clock = std::chrono::steady_clock;

template <unsigned channels, int output_extent>
struct Frame {
    std::vector<std::uint8_t> pixels;
    int width = 0;
    int height = 0;

    void fit(int source_width, int source_height) {
        const int extent = std::max({output_extent, source_width, source_height});
        width = std::max(1, source_width * output_extent / extent);
        height = std::max(1, source_height * output_extent / extent);
        pixels.resize(width * height * channels);
    }
};

using GrayFrame = Frame<1, scan_extent>;
using ColorFrame = Frame<3, snapshot_extent>;

struct YuvMatrix {
    int black, luma, red_v, green_u, green_v, blue_u;

    static YuvMatrix select(bool full_range, bool rec709) {
        if (rec709) {
            return full_range ? YuvMatrix{0, 256, 403, -48, -120, 475}
                              : YuvMatrix{16, 298, 459, -55, -136, 541};
        }
        return full_range ? YuvMatrix{0, 256, 359, -88, -183, 454}
                          : YuvMatrix{16, 298, 409, -100, -208, 516};
    }

    void convert(std::uint8_t* out, int y, int u, int v) const {
        const int base = (y - black) * luma + 128;
        u -= 128;
        v -= 128;
        out[0] = std::clamp((base + red_v * v) / 256, 0, 255);
        out[1] = std::clamp((base + green_u * u + green_v * v) / 256, 0, 255);
        out[2] = std::clamp((base + blue_u * u) / 256, 0, 255);
    }
};

template <unsigned channels, int output_extent>
bool decode_yuyv(const std::uint8_t* bytes, std::size_t size, unsigned width, unsigned height,
                 std::size_t stride, Frame<channels, output_extent>& image, YuvMatrix matrix = {}) {
    if (stride > size || size < stride * (height - 1) + 2 * width) {
        return false;
    }
    if constexpr (channels == 3) {
        if (width % 2 != 0) {
            return false;
        }
    }
    image.fit(width, height);
    for (int row = 0; row < image.height; ++row) {
        const auto* source = bytes + std::size_t(row * height / image.height) * stride;
        for (int column = 0; column < image.width; ++column) {
            const unsigned sample = column * width / image.width;
            auto* out = image.pixels.data() + channels * (row * image.width + column);
            if constexpr (channels == 1) {
                *out = source[2 * sample];
            } else {
                const auto* pair = source + 4 * (sample / 2);
                matrix.convert(out, pair[2 * (sample % 2)], pair[1], pair[3]);
            }
        }
    }
    return true;
}

[[maybe_unused]] bool write_snapshot(const ColorFrame& image, std::FILE* stream) {
    return std::fprintf(stream, "P6\n%d %d\n255\n", image.width, image.height) >= 0 &&
           std::fwrite(image.pixels.data(), 1, image.pixels.size(), stream) == image.pixels.size() &&
           std::fflush(stream) == 0;
}

struct Face {
    bool present = false;
    float x = 0;
    float y = 0;
};

class Detector {
public:
    explicit Detector(const char* path) {
        // The pinned facefinder is little-endian, like the target CPU. Read
        // it into aligned storage and check its expected shape before using
        // the C runtime, whose API receives no byte count.
        std::ifstream file(path, std::ios::binary);
        if (!file.read(reinterpret_cast<char*>(cascade_.data()), sizeof(cascade_)) ||
            file.peek() != std::char_traits<char>::eof() ||
            cascade_[0] != 3 || cascade_[1] != 2139193217 ||
            cascade_[2] != 6 || cascade_[3] != 468) {
            throw std::runtime_error("invalid facefinder cascade");
        }
    }

    Face find(GrayFrame& image) {
        constexpr int capacity = 128;
        std::array<float, 4 * capacity> detections;
        const int count = find_objects(detections.data(), capacity, cascade_.data(), 0.0f,
                                       image.pixels.data(), image.height, image.width, image.width,
                                       1.14f, 0.10f, 30.0f, std::min(image.width, image.height));
        const int clustered = cluster_detections(detections.data(), count);
        int best = -1;
        float score = 5.0f;
        for (int index = 0; index < clustered; ++index) {
            const float quality = detections[4 * index + 3];
            if (quality >= score) {
                score = quality;
                best = index;
            }
        }
        if (best < 0) {
            return {};
        }
        const float row = detections[4 * best];
        const float column = detections[4 * best + 1];
        return {true, 2 * column / image.width - 1, 1 - 2 * row / image.height};
    }

private:
    std::array<std::uint32_t, 239632 / sizeof(std::uint32_t)> cascade_;
};

struct JpegError {
    jpeg_error_mgr manager{};
    std::jmp_buf recovery;
};

void reject_jpeg(j_common_ptr image) {
    auto* error = reinterpret_cast<JpegError*>(image->err);
    std::longjmp(error->recovery, 1);
}

void ignore_jpeg_message(j_common_ptr) {}

class JpegReader {
public:
    template <unsigned channels, int output_extent>
    bool decode(const std::uint8_t* bytes, unsigned long size, Frame<channels, output_extent>& image) {
        if (size == 0) {
            return false;
        }
        decoder_.err = jpeg_std_error(&error_.manager);
        error_.manager.error_exit = reject_jpeg;
        error_.manager.output_message = ignore_jpeg_message;
        if (setjmp(error_.recovery)) {
            if (standard_.mem != nullptr) {
                jpeg_destroy_compress(&standard_);
            }
            jpeg_destroy_decompress(&decoder_);
            return false;
        }
        jpeg_create_decompress(&decoder_);
        jpeg_mem_src(&decoder_, bytes, size);
        if (jpeg_read_header(&decoder_, TRUE) != JPEG_HEADER_OK ||
            decoder_.image_width < 2 || decoder_.image_height < 2 ||
            decoder_.image_width > max_capture_width || decoder_.image_height > max_capture_height) {
            jpeg_destroy_decompress(&decoder_);
            return false;
        }
        // Some UVC MJPEG streams omit the standard Huffman tables.
        if (decoder_.dc_huff_tbl_ptrs[0] == nullptr) {
            load_default_tables();
        }
        const unsigned extent = std::max(decoder_.image_width, decoder_.image_height);
        decoder_.scale_denom = extent >= 4 * output_extent ? 4 : extent >= 2 * output_extent ? 2 : 1;
        decoder_.out_color_space = channels == 1 ? JCS_GRAYSCALE : JCS_RGB;
        jpeg_start_decompress(&decoder_);
        image.fit(decoder_.output_width, decoder_.output_height);
        const unsigned source_width = decoder_.output_width;
        const unsigned source_height = decoder_.output_height;
        JSAMPARRAY scanline = decoder_.mem->alloc_sarray(reinterpret_cast<j_common_ptr>(&decoder_),
                                                       JPOOL_IMAGE, channels * source_width, 1);
        int row = 0;
        while (decoder_.output_scanline < source_height) {
            const unsigned current = decoder_.output_scanline;
            jpeg_read_scanlines(&decoder_, scanline, 1);
            if (row < image.height && current == row * source_height / image.height) {
                for (int column = 0; column < image.width; ++column) {
                    const auto* in = scanline[0] + channels * (column * source_width / image.width);
                    auto* out = image.pixels.data() + channels * (row * image.width + column);
                    std::copy_n(in, channels, out);
                }
                ++row;
            }
        }
        jpeg_finish_decompress(&decoder_);
        jpeg_destroy_decompress(&decoder_);
        // libjpeg can recover from short input. Discard the resulting partial
        // image rather than turning its synthetic padding into a detection.
        return error_.manager.num_warnings == 0;
    }

private:
    void load_default_tables() {
        standard_.err = decoder_.err;
        jpeg_create_compress(&standard_);
        standard_.in_color_space = JCS_YCbCr;
        standard_.input_components = 3;
        jpeg_set_defaults(&standard_);
        for (int index = 0; index < 2; ++index) {
            if (decoder_.dc_huff_tbl_ptrs[index] == nullptr) {
                decoder_.dc_huff_tbl_ptrs[index] = jpeg_alloc_huff_table(reinterpret_cast<j_common_ptr>(&decoder_));
                *decoder_.dc_huff_tbl_ptrs[index] = *standard_.dc_huff_tbl_ptrs[index];
            }
            if (decoder_.ac_huff_tbl_ptrs[index] == nullptr) {
                decoder_.ac_huff_tbl_ptrs[index] = jpeg_alloc_huff_table(reinterpret_cast<j_common_ptr>(&decoder_));
                *decoder_.ac_huff_tbl_ptrs[index] = *standard_.ac_huff_tbl_ptrs[index];
            }
        }
        jpeg_destroy_compress(&standard_);
    }

    JpegError error_;
    jpeg_decompress_struct decoder_{};
    jpeg_compress_struct standard_{};
};

#ifdef __linux__

[[noreturn]] void system_error(const char* message) {
    throw std::system_error(errno, std::generic_category(), message);
}

class Descriptor {
public:
    explicit Descriptor(const char* path) : value(open(path, O_RDWR | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW)) {
        if (value < 0) {
            system_error("open camera");
        }
    }
    ~Descriptor() { close(value); }
    Descriptor(const Descriptor&) = delete;
    Descriptor& operator=(const Descriptor&) = delete;
    const int value;
};

struct Mapping {
    void* address = MAP_FAILED;
    std::size_t length = 0;
    ~Mapping() {
        if (address != MAP_FAILED) {
            munmap(address, length);
        }
    }
};

const char* authorised_path(const char* path, const char* usb_parent) {
    if (usb_parent) {
        const std::string parent(usb_parent);
        std::ifstream policy(parent + "/authorized");
        std::string allowed;
        if (parent.compare(0, 13, "/sys/devices/") != 0 || !(policy >> allowed) || allowed != "1") {
            throw std::runtime_error("USB camera authorisation is no longer active");
        }
    }
    return path;
}

class Camera {
public:
    Camera(const char* path, const char* usb_parent) : fd_(authorised_path(path, usb_parent)) {
        struct stat info{};
        if (fstat(fd_.value, &info) != 0) {
            system_error("inspect camera device");
        }
        if (!S_ISCHR(info.st_mode)) {
            throw std::runtime_error("camera path is not a character device");
        }
        if (usb_parent) {
            // Check the device actually opened, before requesting any frames.
            const std::string number = std::to_string(major(info.st_rdev)) + ":" + std::to_string(minor(info.st_rdev));
            const auto device = std::filesystem::canonical("/sys/dev/char/" + number + "/device");
            const std::string parent(usb_parent);
            const std::string child = device.string();
            if (child != parent && child.compare(0, parent.size() + 1, parent + "/") != 0) {
                throw std::runtime_error("opened video device is outside the authorised camera");
            }
            authorised_path(path, usb_parent);
        }
        v4l2_capability caps{};
        checked_ioctl(VIDIOC_QUERYCAP, &caps, "query camera capabilities");
        const auto flags = (caps.capabilities & V4L2_CAP_DEVICE_CAPS) ? caps.device_caps : caps.capabilities;
        if (!(flags & V4L2_CAP_VIDEO_CAPTURE) || !(flags & V4L2_CAP_STREAMING)) {
            throw std::runtime_error("camera lacks single-plane V4L2 streaming capture");
        }
        format_ = negotiate_format();
        // Some cameras fix the frame rate. The detection cadence stays capped
        // even if the device declines the five frames per second request.
        v4l2_streamparm params{};
        params.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        params.parm.capture.timeperframe.numerator = 1;
        params.parm.capture.timeperframe.denominator = 5;
        call_ioctl(VIDIOC_S_PARM, &params);
        authorised_path(path, usb_parent);
        start_stream();
    }

    ~Camera() {
        v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        call_ioctl(VIDIOC_STREAMOFF, &type);
    }

    template <unsigned channels, int output_extent>
    bool frame(Frame<channels, output_extent>& image, Clock::time_point sample_after,
               Clock::time_point deadline = Clock::now() + frame_timeout) {
        for (;;) {
            const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - Clock::now());
            if (left.count() <= 0) {
                throw std::runtime_error("camera stopped sending frames");
            }
            pollfd poll_state{fd_.value, POLLIN, 0};
            const int ready = poll(&poll_state, 1, static_cast<int>(left.count()));
            if (ready < 0 && errno == EINTR) {
                continue;
            }
            if (ready < 0) {
                system_error("wait for camera frames");
            }
            if (ready == 0) {
                throw std::runtime_error("camera stopped sending frames");
            }
            if (poll_state.revents & (POLLERR | POLLHUP | POLLNVAL)) {
                throw std::runtime_error("camera stream disconnected");
            }

            v4l2_buffer buffer{};
            buffer.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            buffer.memory = V4L2_MEMORY_MMAP;
            if (call_ioctl(VIDIOC_DQBUF, &buffer) < 0) {
                if (errno == EAGAIN) {
                    continue;
                }
                system_error("receive camera frame");
            }
            const bool sample = Clock::now() >= sample_after;
            const bool decoded = sample && decode(buffer, image);
            checked_ioctl(VIDIOC_QBUF, &buffer, "return camera buffer");
            if (sample) {
                return decoded;
            }
        }
    }

private:
    int call_ioctl(unsigned long command, void* argument) const {
        int status;
        do {
            status = ioctl(fd_.value, command, argument);
        } while (status < 0 && errno == EINTR);
        return status;
    }

    void checked_ioctl(unsigned long command, void* argument, const char* message) const {
        if (call_ioctl(command, argument) < 0) {
            system_error(message);
        }
    }

    v4l2_pix_format negotiate_format() const {
        for (const auto encoding : {V4L2_PIX_FMT_YUYV, V4L2_PIX_FMT_MJPEG, V4L2_PIX_FMT_JPEG}) {
            v4l2_format request{};
            request.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            auto& desired = request.fmt.pix;
            desired.width = 320;
            desired.height = 240;
            desired.pixelformat = encoding;
            desired.field = V4L2_FIELD_ANY;
            if (call_ioctl(VIDIOC_S_FMT, &request) < 0) {
                continue;
            }
            if ((desired.pixelformat == V4L2_PIX_FMT_YUYV || desired.pixelformat == V4L2_PIX_FMT_MJPEG ||
                 desired.pixelformat == V4L2_PIX_FMT_JPEG) &&
                desired.width >= 2 && desired.height >= 2 &&
                desired.width <= max_capture_width && desired.height <= max_capture_height) {
                return desired;
            }
        }
        throw std::runtime_error("camera cannot stream YUYV or MJPEG at a supported resolution");
    }

    void start_stream() {
        v4l2_requestbuffers request{};
        request.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        request.memory = V4L2_MEMORY_MMAP;
        request.count = 4;
        checked_ioctl(VIDIOC_REQBUFS, &request, "allocate camera buffers");
        if (request.count < 2 || request.count > buffers_.size()) {
            throw std::runtime_error("unsupported camera buffer count");
        }
        count_ = request.count;
        for (unsigned index = 0; index < count_; ++index) {
            v4l2_buffer buffer{};
            buffer.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            buffer.memory = V4L2_MEMORY_MMAP;
            buffer.index = index;
            checked_ioctl(VIDIOC_QUERYBUF, &buffer, "query camera buffer");
            auto& mapping = buffers_[index];
            mapping.length = buffer.length;
            if (mapping.length == 0 || mapping.length > 16 * 1024 * 1024) {
                throw std::runtime_error("unsupported camera buffer size");
            }
            mapping.address = mmap(nullptr, mapping.length, PROT_READ | PROT_WRITE, MAP_SHARED,
                                   fd_.value, buffer.m.offset);
            if (mapping.address == MAP_FAILED) {
                system_error("map camera buffer");
            }
            checked_ioctl(VIDIOC_QBUF, &buffer, "queue camera buffer");
        }
        v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        checked_ioctl(VIDIOC_STREAMON, &type, "start camera stream");
    }

    template <unsigned channels, int output_extent>
    bool decode(const v4l2_buffer& buffer, Frame<channels, output_extent>& image) {
        if (buffer.index >= count_) {
            throw std::runtime_error("camera returned an unknown buffer");
        }
        const auto& mapping = buffers_[buffer.index];
        if (buffer.bytesused > mapping.length || buffer.flags & V4L2_BUF_FLAG_ERROR) {
            return false;
        }
        const auto* bytes = static_cast<const std::uint8_t*>(mapping.address);
        if (format_.pixelformat != V4L2_PIX_FMT_YUYV) {
            return jpeg_.decode(bytes, buffer.bytesused, image);
        }

        const auto stride = std::max(format_.bytesperline, 2 * format_.width);
        if constexpr (channels == 1) {
            return decode_yuyv(bytes, buffer.bytesused, format_.width, format_.height, stride, image);
        } else {
            const auto encoding = format_.ycbcr_enc == V4L2_YCBCR_ENC_DEFAULT
                ? V4L2_MAP_YCBCR_ENC_DEFAULT(format_.colorspace) : format_.ycbcr_enc;
            const auto quantization = format_.quantization == V4L2_QUANTIZATION_DEFAULT
                ? V4L2_MAP_QUANTIZATION_DEFAULT(false, format_.colorspace, encoding) : format_.quantization;
            const auto matrix = YuvMatrix::select(quantization == V4L2_QUANTIZATION_FULL_RANGE,
                encoding == V4L2_YCBCR_ENC_709 || encoding == V4L2_YCBCR_ENC_XV709);
            return decode_yuyv(bytes, buffer.bytesused, format_.width, format_.height, stride, image, matrix);
        }
    }

    Descriptor fd_;
    std::array<Mapping, 8> buffers_{};
    unsigned count_ = 0;
    v4l2_pix_format format_{};
    JpegReader jpeg_;
};

void capture(const char* device, const char* cascade, const char* usb_parent) {
    Detector detector(cascade);
    Camera camera(device, usb_parent);
    GrayFrame image;
    auto next = Clock::now();
    unsigned invalid = 0;
    for (;;) {
        Face face;
        if (camera.frame(image, next)) {
            invalid = 0;
            face = detector.find(image);
        } else if (++invalid == 5) {
            throw std::runtime_error("camera repeatedly returned invalid frames");
        }
        if (face.present) {
            std::printf("{\"present\":true,\"x\":%.4f,\"y\":%.4f}\n", face.x, face.y);
        } else {
            std::puts("{\"present\":false}");
        }
        if (std::fflush(stdout) != 0) {
            return;
        }
        next = Clock::now() + scan_interval;
    }
}

void snapshot(const char* device, const char* usb_parent) {
    ColorFrame image;
    {
        // This command gets a new stream and fresh capture buffers after the
        // request. It cannot sample the presence worker's old queue.
        Camera camera(device, usb_parent);
        const auto deadline = Clock::now() + frame_timeout;
        unsigned invalid = 0;
        while (!camera.frame(image, Clock::time_point::min(), deadline)) {
            if (++invalid == 5) {
                throw std::runtime_error("camera repeatedly returned invalid frames");
            }
        }
    }
    // Stop capture and release the device before writing to a possibly slow pipe.
    if (!write_snapshot(image, stdout)) {
        throw std::runtime_error("write snapshot");
    }
}

#endif

}  // namespace

int main(int argc, char** argv) {
    constexpr auto usage = "Usage: eyes-capture --device /dev/video0 --cascade /usr/share/being/facefinder [--usb-parent PATH]\n"
                           "       eyes-capture --snapshot --device /dev/video0 [--usb-parent PATH]";
    if (argc == 2 && std::string_view(argv[1]) == "--help") {
        std::puts(usage);
        return 0;
    }
    const char* device = nullptr;
    const char* cascade = nullptr;
    const char* usb_parent = nullptr;
    bool take_snapshot = false;
    for (int i = 1; i < argc; ++i) {
        const std::string_view option = argv[i];
        if (option == "--snapshot" && !take_snapshot) {
            take_snapshot = true;
        } else if (option == "--device" && device == nullptr && i + 1 < argc) {
            device = argv[++i];
        } else if (option == "--cascade" && cascade == nullptr && i + 1 < argc) {
            cascade = argv[++i];
        } else if (option == "--usb-parent" && usb_parent == nullptr && i + 1 < argc) {
            usb_parent = argv[++i];
        } else {
            std::fprintf(stderr, "%s\n", usage);
            return 2;
        }
    }
    if (!device || (take_snapshot == (cascade != nullptr))) {
        std::fprintf(stderr, "%s\n", usage);
        return 2;
    }
    try {
#ifdef __linux__
        if (take_snapshot) {
            snapshot(device, usb_parent);
        } else {
            capture(device, cascade, usb_parent);
        }
#else
        throw std::runtime_error("V4L2 camera capture requires Linux");
#endif
    } catch (const std::exception& error) {
        std::fprintf(stderr, "eyes-capture: %s\n", error.what());
        return 1;
    }
    return 0;
}
