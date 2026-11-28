#pragma once

#include <QJsonObject>
#include <QObject>
#include <QProcess>
#include <QTimer>

#include <functional>

inline constexpr int protocolVersion = 2;
inline constexpr qsizetype frameLimit = 1024 * 1024;

QByteArray requestFrame(qint64 id, const QString &method, const QJsonObject &params);
QJsonObject responseFrame(const QByteArray &line);

class ServiceClient final : public QObject {
public:
    using Complete = std::function<void(const QJsonObject &, const QString &)>;
    using Progress = std::function<void(const QString &, qint64, qint64)>;

    ServiceClient(QString root, QString python, QObject *parent);
    void start();
    void stop();
    bool available() const;
    bool busy() const { return active_ != 0; }
    void send(const QString &method, const QJsonObject &params, Complete complete);
    void cancel();
    std::function<void()> onReady;
    std::function<void(const QString &)> onFailure;
    Progress onProgress;

private:
    friend struct ServiceClientTest;

    void read();
    void consume(const QByteArray &line);
    qint64 write(const QString &method, const QJsonObject &params);
    void fail(const QString &reason);

    const QString root_;
    const QString python_;
    QProcess process_;
    QTimer startup_;
    QTimer inactivity_;
    QByteArray buffer_;
    QJsonObject lastProgress_;
    qint64 sequence_ = 0;
    qint64 hello_ = 0;
    qint64 active_ = 0;
    qint64 cancel_ = 0;
    bool ready_ = false;
    bool stopping_ = false;
    bool waitingForTransfer_ = false;
    bool timingOut_ = false;
    Complete complete_;
};
