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
class QListView;
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
    ~Window() override;

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
    QJsonObject selectionScope() const;
    QJsonObject selectionFilters() const;
    void editSelection(const QString &action, QJsonObject params = {});
    void selectionPreview();
    void selectionFilterDialog();
    void preview();
    void stopPlayer();
    void plan();
    void prepare();
    void setPlan(const QJsonObject &plan);
    QJsonObject recoveryOptions() const;
    void showHistory();
    void operationDiagnostic(bool privateDetails);
    void operationBundle();
    void storage();
    void verify(bool snapshots);
    void saveValidationDiagnostic();
    void updates(bool prompt);
    void showAccount();
    void accountDialog(const QJsonObject &state);
    void answerLogin(const QString &scope, const QJsonObject &result);
    void selectTarget(int offset = 0, const QString &query = {}, bool refresh = true);
    void transfer(const QString &target, const QString &name);
    void transferActions(const QString &target, const QString &name, const QJsonObject &profile);
    void startImport(const QJsonObject &review);
    void repairRejectedMedia(const QJsonObject &params);
    void task(const QString &name, const QString &method, const QJsonObject &params,
              std::function<void(const QJsonObject &)> done);
    void status(const QString &message);
    QString fault(const QString &code) const;

    const QString root_;
    const QString python_;
    ServiceClient service_;
    QString project_;
    QString bundle_;
    QString historyBundle_;
    QJsonObject projectData_;
    QJsonObject plan_;
    QJsonObject decisions_;
    QJsonObject selectedRecord_;
    QJsonObject lastDiagnostic_;
    QJsonObject rejectedMedia_;
    QJsonObject selectionFilters_;
    QString selectionRevision_;
    QString manifest_;
    QString testTarget_;
    QString testBundle_;
    qint64 cursor_ = 0;
    int currentStep_ = Source;
    QListWidget *nav_;
    QStackedWidget *pages_;
    QLabel *account_;
    QLabel *status_;
    QProgressBar *progress_;
    QPushButton *cancel_;
    QPushButton *restart_;
    QPushButton *saveDiagnostic_;
    QPushButton *update_;
    QListWidget *recent_;
    QLabel *overview_;
    QComboBox *chat_;
    QLineEdit *query_;
    QListView *recordList_;
    QComboBox *selectionPreset_;
    QComboBox *selectionView_;
    QPushButton *undoSelection_;
    QPushButton *redoSelection_;
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
