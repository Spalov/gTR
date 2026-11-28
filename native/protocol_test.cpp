#include "service_client.hpp"

#include <QCoreApplication>
#include <QJsonDocument>

namespace {
const QByteArray attachmentWait =
    R"({"v":2,"id":8,"event":"progress","stage":"upload_file_parts","done":0,"total":0,"wait_seconds":833})";
const QByteArray attachmentBytes =
    R"({"v":2,"id":8,"event":"progress","stage":"upload_bytes","done":4000000,"total":93490660})";
}

struct ServiceClientTest {
    static bool transferDeadline() {
        ServiceClient service({}, {}, nullptr);
        service.active_ = 8;
        service.consume(attachmentWait);
        if (!service.inactivity_.isActive() || service.inactivity_.interval() != 863000)
            return false;
        const auto deadline = service.inactivity_.id();
        service.consume(attachmentBytes);
        if (service.inactivity_.id() != deadline)
            return false;
        service.consume(R"({"v":2,"id":8,"event":"progress","stage":"bind_media","done":48,"total":218})");
        if (service.inactivity_.interval() != 180000)
            return false;

        service.cancel_ = 9;
        if (!QMetaObject::invokeMethod(&service.inactivity_, "timeout") || service.inactivity_.interval() != 10000)
            return false;
        service.consume(attachmentBytes);
        if (service.inactivity_.interval() != 10000)
            return false;
        bool timedOut = false;
        service.complete_ = [&](const QJsonObject &data, const QString &error) {
            timedOut = error == "IPC_TIMEOUT" && data["stage"].toString() == "upload_bytes";
        };
        service.consume(R"({"v":2,"id":8,"error":{"code":"CANCELLED"}})");
        return timedOut && !service.inactivity_.isActive();
    }
};

int main(int argc, char **argv) {
    QCoreApplication app(argc, argv);
    const auto request = QJsonDocument::fromJson(requestFrame(8, "chat.page", {{"chat", 42}})).object();
    if (request["v"].toInt() != protocolVersion || request["id"].toInt() != 8
        || request["method"].toString() != "chat.page" || request["params"].toObject()["chat"].toInt() != 42)
        return 1;
    if (responseFrame(R"({"v":2,"id":8,"result":{"before":null}})").isEmpty())
        return 2;
    if (responseFrame(R"({"v":9,"id":8,"result":{}})").size()
        || responseFrame(QByteArray(frameLimit + 1, 'x')).size()
        || responseFrame(R"({"v":2,"id":"8","result":{}})").size()
        || responseFrame(R"({"v":2,"id":8,"error":{"code":"BUSY"},"result":{}})").size()
        || responseFrame(R"({"v":2,"id":8,"event":"progress","stage":"a"})").size())
        return 3;
    if (responseFrame(R"({"v":2,"id":8,"event":"progress","stage":"files","done":1,"total":2})").isEmpty()
        || responseFrame(R"({"v":2,"id":8,"error":{"code":"CANCELLED"}})").isEmpty())
        return 4;
    if (responseFrame(attachmentWait)["wait_seconds"].toInt() != 833 || responseFrame(attachmentBytes).isEmpty())
        return 5;
    if (!responseFrame(R"({"v":2,"id":8,"event":"progress","stage":"upload_file_parts","done":0,"total":0,"wait_seconds":1.5})").isEmpty()
        || !responseFrame(R"({"v":2,"id":8,"event":"progress","stage":"upload_file_parts","done":0,"total":0,"wait_seconds":3601})").isEmpty()
        || !responseFrame(R"({"v":2,"id":8,"event":"progress","stage":"upload_bytes","done":1,"total":2,"wait_seconds":3600})").isEmpty()
        || !responseFrame(R"({"v":2,"id":8,"result":{},"wait_seconds":181})").isEmpty())
        return 6;
    if (!ServiceClientTest::transferDeadline())
        return 7;
}
