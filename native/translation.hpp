#pragma once

#include <QCoreApplication>
#include <QTranslator>

inline QString B(const char *ru, const char *uk) {
    return QCoreApplication::translate("gTR", ru, uk);
}

class Ukrainian final : public QTranslator {
public:
    QString translate(const char *context, const char *, const char *uk, int = -1) const override {
        return QByteArrayView(context) == QByteArrayView("gTR") && uk ? QString::fromUtf8(uk) : QString{};
    }
    bool isEmpty() const override { return false; }
};
