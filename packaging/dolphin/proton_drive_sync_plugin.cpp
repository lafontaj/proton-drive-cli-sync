/*
    SPDX-FileCopyrightText: 2026 Proton Drive Sync contributors
    SPDX-License-Identifier: MIT
*/

#include "proton_drive_sync_plugin.h"

#include <QDBusConnection>
#include <QDBusInterface>
#include <QDBusMessage>
#include <QDBusArgument>
#include <QDir>
#include <QFileInfo>
#include <QVariantMap>

#include <QDateTime>

namespace {

const int kTimeoutMs = 500;
const qint64 kCacheMs = 2000;

QStringList iconForEmblem(const QString &emblem)
{
    // Names are cloudstatus.EMBLEMS. Dolphin draws these as overlay icons.
    if (emblem == QLatin1String("emblem-default")) {
        return {QStringLiteral("vcs-normal")};
    }
    if (emblem == QLatin1String("emblem-synchronizing")) {
        return {QStringLiteral("vcs-update-required")};
    }
    if (emblem == QLatin1String("emblem-important")) {
        return {QStringLiteral("vcs-conflicting")};
    }
    if (emblem == QLatin1String("emblem-new")) {
        return {QStringLiteral("vcs-locally-modified")};
    }
    return {};
}

QDBusMessage callStatus(const QString &method, const QString &argument)
{
    if (!QDBusConnection::sessionBus().isConnected()) {
        return QDBusMessage();
    }
    QDBusInterface bus(QStringLiteral("org.protondrivesync.CloudProviders"),
                       QStringLiteral("/org/protondrivesync/CloudProviders"),
                       QStringLiteral("org.protondrivesync.FileStatus"),
                       QDBusConnection::sessionBus());
    if (!bus.isValid()) {
        return QDBusMessage();
    }
    bus.setTimeout(kTimeoutMs);
    return bus.call(method, argument);
}

}

ProtonDriveSyncPlugin::ProtonDriveSyncPlugin(QObject *parent)
    : KOverlayIconPlugin(parent)
{
    QDBusConnection::sessionBus().connect(
        QStringLiteral("org.protondrivesync.CloudProviders"),
        QStringLiteral("/org/protondrivesync/CloudProviders"),
        QStringLiteral("org.protondrivesync.FileStatus"),
        QStringLiteral("StatusChanged"),
        this, SLOT(onStatusChanged()));
}

void ProtonDriveSyncPlugin::onStatusChanged()
{
    QStringList paths = m_byDirectory.keys();
    paths += m_byPath.keys();
    m_fetchedAt.clear();
    m_pathFetchedAt.clear();
    for (const QString &path : paths) {
        const QUrl url = QUrl::fromLocalFile(path);
        Q_EMIT overlaysChanged(url, getOverlays(url));
    }
}

QStringList ProtonDriveSyncPlugin::getOverlays(const QUrl &item)
{
    if (!item.isLocalFile()) {
        return {};
    }
    return iconForEmblem(emblemFor(QDir::cleanPath(item.toLocalFile())));
}

QString ProtonDriveSyncPlugin::emblemFor(const QString &path)
{
    const QString directory = QFileInfo(path).path();
    const qint64 now = QDateTime::currentMSecsSinceEpoch();
    if (!m_fetchedAt.contains(directory) || now - m_fetchedAt.value(directory) > kCacheMs) {
        fetchDirectory(directory);
        m_fetchedAt.insert(directory, now);
    }
    if (m_byDirectory.contains(path)) {
        return m_byDirectory.value(path);
    }
    if (m_pathFetchedAt.contains(path) && now - m_pathFetchedAt.value(path) <= kCacheMs) {
        return m_byPath.value(path);
    }
    const QString emblem = fetchPath(path);
    m_byPath.insert(path, emblem);
    m_pathFetchedAt.insert(path, now);
    return emblem;
}

void ProtonDriveSyncPlugin::fetchDirectory(const QString &directory)
{
    QHash<QString, QString> fresh;
    {
            const QDBusMessage reply = callStatus(QStringLiteral("GetDirectoryStatus"), directory);
            if (reply.type() == QDBusMessage::ReplyMessage && !reply.arguments().isEmpty()) {
                QMap<QString, QString> emblems;
                const QVariant value = reply.arguments().at(0);
                if (value.canConvert<QDBusArgument>()) {
                    QDBusArgument argument = value.value<QDBusArgument>();
                    argument >> emblems;
                } else {
                    const QVariantMap variantMap = value.toMap();
                    for (auto it = variantMap.cbegin(); it != variantMap.cend(); ++it) {
                        emblems.insert(it.key(), it.value().toString());
                    }
                }
                for (auto it = emblems.cbegin(); it != emblems.cend(); ++it) {
                    fresh.insert(QDir::cleanPath(it.key()), it.value());
                }
            }
    }
    const QString prefix = directory.endsWith(QLatin1Char('/')) ? directory : directory + QLatin1Char('/');
    QStringList stale;
    for (auto it = m_byDirectory.cbegin(); it != m_byDirectory.cend(); ++it) {
        if (it.key().startsWith(prefix)) {
            stale.append(it.key());
        }
    }
    for (const QString &key : stale) {
        m_byDirectory.remove(key);
    }
    for (auto it = fresh.cbegin(); it != fresh.cend(); ++it) {
        m_byDirectory.insert(it.key(), it.value());
    }
}

QString ProtonDriveSyncPlugin::fetchPath(const QString &path)
{
    const QDBusMessage reply = callStatus(QStringLiteral("GetPathEmblem"), path);
    if (reply.type() != QDBusMessage::ReplyMessage || reply.arguments().isEmpty()) {
        return {};
    }
    return reply.arguments().at(0).toString();
}
