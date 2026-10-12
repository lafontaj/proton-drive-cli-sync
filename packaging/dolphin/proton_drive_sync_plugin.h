/*
    SPDX-FileCopyrightText: 2026 Proton Drive Sync contributors
    SPDX-License-Identifier: MIT
*/

#ifndef PROTON_DRIVE_SYNC_PLUGIN_H
#define PROTON_DRIVE_SYNC_PLUGIN_H

#include <KOverlayIconPlugin>

#include <QHash>
#include <QString>
#include <QUrl>

// Overlay marks for mapped files. A missing status bus leaves every file unmarked.
class ProtonDriveSyncPlugin : public KOverlayIconPlugin
{
    Q_OBJECT
    Q_PLUGIN_METADATA(IID "org.kde.overlayicon.protondrivesync")

public:
    explicit ProtonDriveSyncPlugin(QObject *parent = nullptr);

    QStringList getOverlays(const QUrl &item) override;

private Q_SLOTS:
    void onStatusChanged();

private:
    QString emblemFor(const QString &path);
    void fetchDirectory(const QString &directory);
    QString fetchPath(const QString &path);

    QHash<QString, QString> m_byDirectory;
    QHash<QString, qint64> m_fetchedAt;
    QHash<QString, QString> m_byPath;
    QHash<QString, qint64> m_pathFetchedAt;
};

#endif
