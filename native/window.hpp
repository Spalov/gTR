#pragma once

#include "service_client.hpp"

#include <QJsonArray>
#include <QMainWindow>
#include <QModelIndex>

class QAudioOutput;
class QCheckBox;
class QCloseEvent;
class QComboBox;
class QLabel;
class QLineEdit;
class QListWidget;
class QMediaPlayer;
class QProgressBar;
class QPushButton;
class QSlider;
class QStackedWidget;
class QStandardItemModel;
class QTableWidget;
class QVideoWidget;
class QWidget;

class Window final : public QMainWindow {
public:
    Window(QString root, QString python, const QString &initialProject);

private:
    enum Page { Home, Archive, Recovery, History, Settings };
    enum Step { Source, Dialog, Analysis, Preparation, Result };
    void closeEvent(QCloseEvent *event) override;

    QWidget *homePage();
    QWidget *archivePage();
    QWidget *recoveryPage();
    QWidget *historyPage();
    QWidget *settingsPage();
    void go(int page);
    void step(int step);
    void refreshProjects();
    void readProject(const QString &path);
    void chooseSource(bool folder);
    void adoptProject(const QJsonObject &value);
    void selectChat();
    void messages(bool next = false);
    void showMessage(const QModelIndex &index);
    void preview();
    void stopPlayer();
    void plan();
    void prepare();
    void setPlan(const QJsonObject &plan);
    QJsonObject recoveryOptions() const;
    void showHistory();
    void storage();
    void verify(bool snapshots);
    void updates(bool prompt);
    void classic(const QString &section);
    void task(const QString &name, const QString &method, const QJsonObject &params,
              std::function<void(const QJsonObject &)> done);
    void status(const QString &message);
    QString fault(const QString &code) const;

    const QString root_;
    const QString python_;
    ServiceClient service_;
    QString project_;
    QString bundle_;
    QJsonObject projectData_;
    QJsonObject plan_;
    QJsonObject decisions_;
    QJsonObject selectedRecord_;
    qint64 cursor_ = 0;
    int currentStep_ = Source;
    QListWidget *nav_;
    QStackedWidget *pages_;
    QLabel *account_;
    QLabel *status_;
    QProgressBar *progress_;
    QPushButton *cancel_;
    QPushButton *restart_;
    QPushButton *update_;
    QListWidget *recent_;
    QLabel *overview_;
    QComboBox *chat_;
    QLineEdit *query_;
    QStandardItemModel *records_;
    QPushButton *older_;
    QLabel *message_;
    QLabel *picture_;
    QPushButton *preview_;
    QPushButton *play_;
    QSlider *seek_;
    QLabel *duration_;
    QMediaPlayer *player_;
    QVideoWidget *video_;
    QLabel *source_;
    QLabel *dialog_;
    QLabel *analysis_;
    QTableWidget *issues_;
    QPushButton *applyMediaGroup_;
    QLabel *result_;
    QLabel *diagnosis_;
    QListWidget *steps_;
    QStackedWidget *wizard_;
    QWidget *advancedOptions_;
    QComboBox *videoPolicy_;
    QComboBox *noteMode_;
    QComboBox *mediaMode_;
    QPushButton *continue_;
    QTableWidget *history_;
    QCheckBox *advanced_;
    QLabel *storage_;
};
