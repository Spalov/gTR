#include "service_client.hpp"

#include <QJsonDocument>

int main() {
    const auto request = QJsonDocument::fromJson(requestFrame(8, "chat.page", {{"chat", 42}})).object();
    if (request["v"].toInt() != protocolVersion || request["id"].toInt() != 8
        || request["method"].toString() != "chat.page" || request["params"].toObject()["chat"].toInt() != 42)
        return 1;
    if (responseFrame(R"({"v":1,"id":8,"result":{"before":null}})").isEmpty())
        return 2;
    if (responseFrame(R"({"v":9,"id":8,"result":{}})").size()
        || responseFrame(QByteArray(frameLimit + 1, 'x')).size()
        || responseFrame(R"({"v":1,"id":"8","result":{}})").size()
        || responseFrame(R"({"v":1,"id":8,"error":{"code":"BUSY"},"result":{}})").size()
        || responseFrame(R"({"v":1,"id":8,"event":"progress","stage":"a"})").size())
        return 3;
    if (responseFrame(R"({"v":1,"id":8,"event":"progress","stage":"files","done":1,"total":2})").isEmpty()
        || responseFrame(R"({"v":1,"id":8,"error":{"code":"CANCELLED"}})").isEmpty())
        return 4;
}
